"""
Name: models.py
Desc: This script defines the entire networks in dewarping films.
"""
import torch
import torch.nn as nn
import torch.nn.functional as F
import modules
import numpy as np
import modules_liu
from tutils import *

from dataloader.bw_mapping import bw_mapping_tensor_batch
from dataloader.bw2deform import deform2bw_tensor_batch

from dataloader.data_process import reprocess_t2t_auto

constrain_path = {
    ('threeD', 'normal'): (True, True, ''),
    ('threeD', 'depth'): (True, True, ''),
    ('normal', 'depth'): (True, True, ''),
    ('depth', 'normal'): (True, True, ''),

}

class UnwarpNet(nn.Module):
    def __init__(self, use_simple=False, combine_num=3, use_constrain=True, constrain_configure=None, use_stn=False, use_deform=False):
        super(UnwarpNet, self).__init__()
        self.combine_num = combine_num
        self.use_simple = use_simple
        self.use_constrain = use_constrain
        self.constrain_configure = constrain_configure
        self.use_stn = use_stn
        self.use_deform = use_deform
        if constrain_configure is None:
            self.constrain_configure = constrain_path
        if use_simple:
            self.geo_encoder = modules.Encoder_sim(downsample=4, in_channels=3)
            self.threeD_decoder = modules.Decoder_sim(downsample=4, out_channels=3)
            self.normal_decoder = modules.Decoder_sim(downsample=4, out_channels=3)
            self.depth_decoder = modules.Decoder_sim(downsample=4, out_channels=1)
            self.mask_decoder = modules.Decoder_sim(downsample=4, out_channels=1)
            self.second_encoder = modules.Encoder_sim(downsample=4, in_channels=3 * 2 ** 6)
            self.uv_decoder = modules.Decoder_sim(downsample=4, out_channels=2)
            self.albedo_decoder = modules.Decoder_sim(downsample=4, out_channels=1)            
        else:
            self.geo_encoder = modules.Encoder(downsample=6, in_channels=3)
            self.threeD_decoder = modules.Decoder(downsample=6, out_channels=3, combine_num=self.combine_num)
            self.normal_decoder = modules.Decoder(downsample=6, out_channels=3, combine_num=self.combine_num)
            self.depth_decoder = modules.Decoder(downsample=6, out_channels=1, combine_num=self.combine_num)
            self.mask_decoder = modules.Decoder(downsample=6, out_channels=1, combine_num=0)
            bottle_neck = sum([2 ** (i + 4) for i in range(self.combine_num)])
            self.second_encoder = modules.Encoder(downsample=6, in_channels=bottle_neck * 3+3)
            self.uv_decoder = modules.Decoder(downsample=6, out_channels=2, combine_num=0)
            #self.albedo_decoder = modules.AlbedoDecoder(downsample=6, out_channels=1)
            self.albedo_decoder = modules.Decoder(downsample=6, out_channels=1,combine_num=0)

            #self.conf_discriminator_enc = modules.Encoder_sim(downsample=6, in_channels=3)
            #self.conf_discriminator_dec = modules.Decoder_sim(downsample=6, in_channels=3)

        if use_constrain:
            if self.constrain_configure['threeD', 'normal'][0] and self.constrain_configure['threeD', 'depth'][0]:
                self.threeD_to_nor2dep = modules.ThreeD2NorDepth(downsample=4, use_simple=True)
                if self.constrain_configure['threeD', 'normal'][1] and self.constrain_configure['threeD', 'depth'][1]\
                        and len(self.constrain_configure['threeD', 'normal'][2]) > 0:
                    self.threeD_to_nor2dep.load_state_dict(torch.load(self.constrain_configure['threeD', 'normal'][2]))
            if self.constrain_configure['normal', 'depth'][0]:
                self.nor2dep = modules.UNetDepth()
                if self.constrain_configure['normal', 'depth'][1] and len(self.constrain_configure['normal', 'depth'][2]) > 0:
                    self.nor2dep.load_state_dict(torch.load(self.constrain_configure['normal', 'depth'][2]))
            if self.constrain_configure['depth', 'normal'][0]:
                self.dep2nor = modules.UNet_sim(downsample=4, in_channels=1, out_channels=3)
                if self.constrain_configure['depth', 'normal'][1] and len(self.constrain_configure['depth', 'normal'][2]) > 0:
                    self.dep2nor.load_state_dict(torch.load(self.constrain_configure['depth', 'normal'][2]))
                # self.dep2nor = modules.UNet(downsample=6, in_channels=1, out_channels=3)
        
        if use_stn:
            # Spatial transformer localization-network
            self.localization = nn.Sequential(
                nn.Conv2d(1024, 512, kernel_size=3),
                nn.ReLU(True)
            )            
            # Regressor for the 3 * 2 affine matrix
            self.fc_loc = nn.Sequential(
                nn.Linear(512*2*2, 512),
                nn.ReLU(True),
                nn.Linear(512, 6*25),
                nn.ReLU(True)
            )
            # Initialize the weights/bias with identity transformation
            # self.fc_loc[2].weight.data.fill_(0)
            # self.fc_loc[2].bias.data = torch.FloatTensor([1, 0, 0, 0, 1, 0])

        if use_deform:
            self.deform_decoder = modules.Decoder(downsample=6, out_channels=2,combine_num=0)   
        

    def forward(self, x):
        gxvals, gx_encode = self.geo_encoder(x)
        threeD_map, threeD_feature = self.threeD_decoder(gxvals, gx_encode)
        threeD_map = nn.functional.tanh(threeD_map)
        dep_map, dep_feature = self.depth_decoder(gxvals, gx_encode)
        dep_map = nn.functional.tanh(dep_map)
        nor_map, nor_feature = self.normal_decoder(gxvals, gx_encode)
        nor_map = nn.functional.tanh(nor_map)
        mask_map, mask_feature = self.mask_decoder(gxvals, gx_encode)
        mask_map = torch.nn.functional.sigmoid(mask_map)
        #geo_feature = torch.cat([threeD_feature, nor_feature, dep_feature], dim=1)
        geo_feature = torch.cat([threeD_feature, nor_feature, dep_feature, x], dim=1)
        b, c, h, w = geo_feature.size()
        geo_feature_mask = geo_feature.mul(mask_map.expand(b, c, h, w))
        secvals, sec_encode = self.second_encoder(geo_feature_mask)

        if self.use_deform:
            df_map, _ = self.deform_decoder(secvals, sec_encode)
        
        uv_map, _ = self.uv_decoder(secvals, sec_encode)
        uv_map = nn.functional.tanh(uv_map)
        #albvals = []
        #for gx, sx in zip(gxvals, secvals):
        #    albvals.append(torch.cat([gx, sx], dim=1))
        #alb_encode = torch.cat([gx_encode, sec_encode], dim=1)
        #alb_map, _ = self.albedo_decoder(albvals, alb_encode)
        alb_map, _ = self.albedo_decoder(secvals, sec_encode)
        alb_map = nn.functional.tanh(alb_map)
      
        if self.use_constrain:
            nor_from_threeD, dep_from_threeD = self.threeD_to_nor2dep(threeD_map)
            nor_from_dep = self.dep2nor(dep_map)
            dep_from_nor = self.nor2dep(nor_map)
            nor_from_threeD = nn.functional.tanh(nor_from_threeD)
            dep_from_threeD = nn.functional.tanh(dep_from_threeD)
            nor_from_dep = nn.functional.tanh(nor_from_dep)
            dep_from_nor = nn.functional.tanh(dep_from_nor)
            if self.use_deform:
                return uv_map, threeD_map, nor_map, alb_map, dep_map, mask_map, \
                    nor_from_threeD, dep_from_threeD, nor_from_dep, dep_from_nor, df_map # (STN)
            else:
                return uv_map, threeD_map, nor_map, alb_map, dep_map, mask_map, \
                    nor_from_threeD, dep_from_threeD, nor_from_dep, dep_from_nor, None
        return uv_map, threeD_map, nor_map, alb_map, dep_map, mask_map, \
                None, None, None, None, None


class Cmap2Fianl(nn.Module):
    def __init__(self):
        super(self, Cmap2Fianl).__init__()
        self.second_encoder = modules.Encoder(downsample=6, in_channels=3)  # 3*4
        self.uv_decoder = modules.Decoder(downsample=6, out_channels=2, combine_num=0)
        #self.albedo_decoder = modules.AlbedoDecoder(downsample=6, out_channels=1)
        self.albedo_decoder = modules.Decoder(downsample=6, out_channels=1,combine_num=0)
        self.deform_decoder = modules.Decoder(downsample=6, out_channels=2, combine_num=0)

    def forward(self, x, ori):
        xvals, x = self.second_encoder(x)
        uv = self.uv_decoder(xvals, x)
        albedo = self.albedo_decoder(xvals, x)
        deform = self.deform_decoder(xvals, x)
        
        bw = deform2bw_tensor_batch(reprocess_t2t_auto(deform, "deform"))
        dewarp_ori = bw_mapping_tensor_batch(reprocess_t2t_auto(ori, "ori"), bw)

        return uv, albedo, deform, bw, dewarp_ori



class Conf_Discriminator(nn.Module):
    def __init__(self):
        super(Conf_Discriminator, self).__init__()
        self.conf_discriminator_enc = modules.Encoder_sim(downsample=4, in_channels=3)
        self.conf_discriminator_dec = modules.Decoder_sim(downsample=4, out_channels=1)
    
    def forward(self, img, dewarp_img):
        bce_loss = nn.BCELoss(reduction='none')
        mse_loss = nn.MSELoss(reduction='none')
        softmax = nn.Softmax(dim=1)
        
        conf_dewarp_inter, b_inter = self.conf_discriminator_enc(dewarp_img)
        conf_dewarp, _ = self.conf_discriminator_dec(conf_dewarp_inter, b_inter)

        conf_img_inter, c_inter = self.conf_discriminator_enc(img)
        conf_img, _ = self.conf_discriminator_dec(conf_img_inter, c_inter)
        #conf_img = softmax(conf_img)

        ones = torch.ones((conf_dewarp.shape[0],conf_dewarp.shape[-2],conf_dewarp.shape[-1]))
        zeros = torch.zeros((conf_dewarp.shape[0],conf_dewarp.shape[-2],conf_dewarp.shape[-1]))
        # confidence discriminator loss
        loss_conf = mse_loss(conf_dewarp, zeros).mean() + mse_loss(conf_img, ones).mean()
        weights = torch.where(conf_dewarp>0., conf_dewarp, torch.zeros_like(conf_dewarp)+.01)
        # total loss weighted by confidence level
        loss_total = (mse_loss(img, dewarp_img) * (1/weights)).mean()

        return loss_conf, loss_total


def stn_loss(result, bw_map):
    print("test loss FUNC")
    print("bw_map: ", bw_map.shape)
    replication = nn.ReplicationPad2d(1)
    bw = replication(bw_map)
    sample = nn.MaxPool2d(kernel_size=1, stride=64)
    bw = sample(bw)
    
    criterion = nn.MSELoss()
    loss = criterion(result, bw)
    return loss


class Net_UVBW(nn.Module):
    def __init__(self):
        super(Net_UVBW, self).__init__()
        # part UV
        self.encoder_uv = modules.Encoder_sim(downsample=4, in_channels=3)
        self.decoder_uv = modules.Decoder_sim(downsample=4, out_channels=2)
        # part BW
        self.encoder_bw = modules.Encoder_sim(downsample=4, in_channels=3)
        self.decoder_bw = modules.Decoder_sim(downsample=4, out_channels=2)
        
    def forward(self, x):
        gxvals1, encode1 = self.encoder_uv(x)
        gxvals2, encode2 = self.encoder_bw(x)
        
        map_uv, feature_uv = self.decoder_uv(gxvals1, encode1)
        map_bw, feature_bw = self.decoder_bw(gxvals2, encode2)
        
        return map_uv, map_bw


if __name__ == '__main__':

    conf = Conf_Discriminator()
    img = torch.ones((3, 3, 256, 256))
    dewarp_img = torch.ones((3, 3, 256, 256))
    loss_conf, loss_total = conf(img, dewarp_img)
    print (loss_conf, loss_total)
    # data_dir ='/home1/qiyuanwang/film_generate/npy'

    # result = torch.ones((32,2,5,5))
    # bw_map = torch.ones((32, 2, 256, 256))
    # loss = stn_loss(result, bw_map)
    # print(loss)
    # exit(0)
    
    # #x = torch.ones((32, 3, 256, 256))
    # #model = UnwarpNet(use_simple=False, combine_num=3, use_constrain=True, constrain_configure=None)
    # #uv_map, threeD_map, nor_map, alb_map, dep_map, mask_map, \
    # #    nor_from_threeD, dep_from_threeD, nor_from_dep, dep_from_nor, result = model(x)
    # #print(uv_map.shape)
    # #print(threeD_map.shape)
    # #print(nor_map.shape)
    # #print(alb_map.shape)
    # #print(dep_map.shape)
    # #print(mask_map.shape)
    # #print(nor_from_threeD.shape)
    # #print(dep_from_threeD.shape)
    # #print(nor_from_dep.shape)
    # #print(dep_from_nor.shape)
    
    # print("----- Extra output --------")
    # print(result.shape)

