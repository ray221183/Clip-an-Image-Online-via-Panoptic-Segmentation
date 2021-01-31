import torch, torchvision
import torch.nn as nn
import torch.nn.functional as F
from torchvision.models.resnet import Bottleneck
import numpy as np
from itertools import product
from math import sqrt
from typing import List
import math
import matplotlib.pyplot as plt


from data.config import cfg, mask_type
from layers import Detect
from layers.interpolate import InterpolateModule
from backbone import construct_backbone

import torch.backends.cudnn as cudnn
from utils import timer
from utils.functions import MovingAverage

# This is required for Pytorch 1.0.1 on Windows to initialize Cuda on some driver versions.
# See the bug report here: https://github.com/pytorch/pytorch/issues/17108
# device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

torch.cuda.current_device()

# As of March 10, 2019, Pytorch DataParallel still doesn't support JIT Script Modules
use_jit = torch.cuda.device_count() <= 1
if not use_jit:
    print('Multiple GPUs detected! Turning off JIT.')

ScriptModuleWrapper = torch.jit.ScriptModule if use_jit else nn.Module
script_method_wrapper = torch.jit.script_method if use_jit else lambda fn, _rcn=None: fn



class Concat(nn.Module):
    def __init__(self, nets, extra_params):
        super().__init__()

        self.nets = nn.ModuleList(nets)
        self.extra_params = extra_params
    
    def forward(self, x):
        # Concat each along the channel dimension
        return torch.cat([net(x) for net in self.nets], dim=1, **self.extra_params)


def make_net(in_channels, conf, include_last_relu=True, list_only=False):
    """
    A helper function to take a config setting and turn it into a network.
    Used by protonet and extrahead. Returns (network, out_channels)
    """
    def make_layer(layer_cfg):
        nonlocal in_channels
        
        # Possible patterns:
        # ( 256, 3, {}) -> conv
        # ( 256,-2, {}) -> deconv
        # (None,-2, {}) -> bilinear interpolate
        # ('cat',[],{}) -> concat the subnetworks in the list
        #
        # You know it would have probably been simpler just to adopt a 'c' 'd' 'u' naming scheme.
        # Whatever, it's too late now.
        if isinstance(layer_cfg[0], str):
            layer_name = layer_cfg[0]

            if layer_name == 'cat':
                nets = [make_net(in_channels, x) for x in layer_cfg[1]]
                layer = Concat([net[0] for net in nets], layer_cfg[2])
                num_channels = sum([net[1] for net in nets])
        else:
            num_channels = layer_cfg[0]
            kernel_size = layer_cfg[1]

            if kernel_size > 0:
                layer = nn.Conv2d(in_channels, num_channels, kernel_size, **layer_cfg[2])
            else:
                if num_channels is None:
                    layer = InterpolateModule(scale_factor=-kernel_size, mode='bilinear', align_corners=False, **layer_cfg[2])
                else:
                    layer = nn.ConvTranspose2d(in_channels, num_channels, -kernel_size, **layer_cfg[2])
        
        in_channels = num_channels if num_channels is not None else in_channels

        # Don't return a ReLU layer if we're doing an upsample. This probably doesn't affect anything
        # output-wise, but there's no need to go through a ReLU here.
        # Commented out for backwards compatibility with previous models
        if num_channels is None:
            return [layer]
        else:
            # return [layer, nn.BatchNorm2d(in_channels) ,nn.ReLU(inplace=True)]
            return [layer, nn.GroupNorm(32, in_channels) ,nn.ReLU(inplace=True)]


    # Use sum to concat together all the component layer lists
    net = sum([make_layer(x) for x in conf], [])
    if not include_last_relu:
        net = net[:-1]

    if list_only is True:
        return net
    else:
        return nn.Sequential(*(net)), in_channels
    


class PredictionModule(nn.Module):
    """
    The (c) prediction module adapted from DSSD:
    https://arxiv.org/pdf/1701.06659.pdf

    Note that this is slightly different to the module in the paper
    because the Bottleneck block actually has a 3x3 convolution in
    the middle instead of a 1x1 convolution. Though, I really can't
    be arsed to implement it myself, and, who knows, this might be
    better.

    Args:
        - in_channels:   The input feature size.
        - out_channels:  The output feature size (must be a multiple of 4).
        - aspect_ratios: A list of lists of priorbox aspect ratios (one list per scale).
        - scales:        A list of priorbox scales relative to this layer's convsize.
                         For instance: If this layer has convouts of size 30x30 for
                                       an image of size 600x600, the 'default' (scale
                                       of 1) for this layer would produce bounding
                                       boxes with an area of 20x20px. If the scale is
                                       .5 on the other hand, this layer would consider
                                       bounding boxes with area 10x10px, etc.
        - parent:        If parent is a PredictionModule, this module will use all the layers
                         from parent instead of from this module.
    """
    
    def __init__(self, in_channels, out_channels=1024, aspect_ratios=[[1]], scales=[1], parent=None):
        super().__init__()

        self.num_classes = cfg.num_classes
        self.mask_dim    = cfg.mask_dim
        self.num_priors  = sum(len(x) for x in aspect_ratios)
        self.parent      = [parent] # Don't include this in the state dict

        if cfg.mask_proto_prototypes_as_features:
            in_channels += self.mask_dim
        
        if parent is None:
            if cfg.extra_head_net is None:
                out_channels = in_channels
            else:
                self.upfeature, out_channels = make_net(in_channels, cfg.extra_head_net)

            if cfg.use_prediction_module:
                self.block = Bottleneck(out_channels, out_channels // 4)
                self.conv = nn.Conv2d(out_channels, out_channels, kernel_size=1, bias=True)
                # self.bn = nn.BatchNorm2d(out_channels)
                self.bn = nn.GroupNorm(32, out_channels)

            self.bbox_layer = nn.Conv2d(out_channels, self.num_priors * 4,                **cfg.head_layer_params)
            self.conf_layer = nn.Conv2d(out_channels, self.num_priors * self.num_classes, **cfg.head_layer_params)
            self.mask_layer = nn.Conv2d(out_channels, self.num_priors * self.mask_dim,    **cfg.head_layer_params)

            if cfg.use_instance_coeff:
                self.inst_layer = nn.Conv2d(out_channels, self.num_priors * cfg.num_instance_coeffs, **cfg.head_layer_params)
            
            # What is this ugly lambda doing in the middle of all this clean prediction module code?
            def make_extra(num_layers):
                if num_layers == 0:
                    return lambda x: x
                else:
                    # Looks more complicated than it is. This just creates an array of num_layers alternating conv-relu
                    return nn.Sequential(*sum([[
                        nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1),
                        nn.ReLU(inplace=True)
                    ] for _ in range(num_layers)], []))

            self.bbox_extra, self.conf_extra, self.mask_extra = [make_extra(x) for x in cfg.extra_layers]
            
            if cfg.mask_type == mask_type.lincomb and cfg.mask_proto_coeff_gate:
                self.gate_layer = nn.Conv2d(out_channels, self.num_priors * self.mask_dim, kernel_size=3, padding=1)

        self.aspect_ratios = aspect_ratios
        self.scales = scales

        self.priors = None
        self.last_conv_size = None

    def forward(self, x):
        """
        Args:
            - x: The convOut from a layer in the backbone network
                 Size: [batch_size, in_channels, conv_h, conv_w])

        Returns a tuple (bbox_coords, class_confs, mask_output, prior_boxes) with sizes
            - bbox_coords: [batch_size, conv_h*conv_w*num_priors, 4]
            - class_confs: [batch_size, conv_h*conv_w*num_priors, num_classes]
            - mask_output: [batch_size, conv_h*conv_w*num_priors, mask_dim]
            - prior_boxes: [conv_h*conv_w*num_priors, 4]
        """
        # In case we want to use another module's layers
        src = self if self.parent[0] is None else self.parent[0]
        
        conv_h = x.size(2)
        conv_w = x.size(3)
        
        if cfg.extra_head_net is not None:
            x = src.upfeature(x)
        
        if cfg.use_prediction_module:
            # The two branches of PM design (c)
            a = src.block(x)
            
            b = src.conv(x)
            b = src.bn(b)
            b = F.relu(b)
            
            # TODO: Possibly switch this out for a product
            x = a + b

        # print("x ", x.size())

        bbox_x = src.bbox_extra(x)
        conf_x = src.conf_extra(x)
        mask_x = src.mask_extra(x)

        bbox_pre = src.bbox_layer(bbox_x)
        conf_pre = src.conf_layer(conf_x)
        # print("bbox_pre ", bbox_pre.size())
        # print("conf_pre ", conf_pre.size())

        bbox = bbox_pre.permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, 4)
        conf = conf_pre.permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, self.num_classes)



        if cfg.eval_mask_branch:
            mask_pre = src.mask_layer(mask_x)
            # print("mask_pre ", mask_pre.size())
            mask = mask_pre.permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, self.mask_dim)
        else:
            mask = torch.zeros(x.size(0), bbox.size(1), self.mask_dim, device=bbox.device)

        # print("bbox ", bbox.size())
        # print("conf ", conf.size())
        # print("mask ", mask.size())
        if cfg.use_instance_coeff:
            inst = src.inst_layer(x).permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, cfg.num_instance_coeffs)

        # See box_utils.decode for an explanation of this
        if cfg.use_yolo_regressors:
            bbox[:, :, :2] = torch.sigmoid(bbox[:, :, :2]) - 0.5
            bbox[:, :, 0] /= conv_w
            bbox[:, :, 1] /= conv_h

        if cfg.eval_mask_branch:
            if cfg.mask_type == mask_type.direct:
                mask = torch.sigmoid(mask)
            elif cfg.mask_type == mask_type.lincomb:
                mask = cfg.mask_proto_coeff_activation(mask)

                if cfg.mask_proto_coeff_gate:
                    gate = src.gate_layer(x).permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, self.mask_dim)
                    mask = mask * torch.sigmoid(gate)
        
        priors = self.make_priors(conv_h, conv_w)

        preds = { 'loc': bbox, 'conf': conf, 'mask': mask, 'priors': priors }

        if cfg.use_instance_coeff:
            preds['inst'] = inst
        
        # print("loc ", preds['loc'].size())
        # print("conf ", preds['conf'].size())
        # print("mask ", preds['mask'].size())
        # print("priors ", preds['priors'].size())

        return preds
    
    def make_priors(self, conv_h, conv_w):
        """ Note that priors are [x,y,width,height] where (x,y) is the center of the box. """
        
        with timer.env('makepriors'):
            if self.last_conv_size != (conv_w, conv_h):
                prior_data = []

                # Iteration order is important (it has to sync up with the convout)
                for j, i in product(range(conv_h), range(conv_w)):
                    # +0.5 because priors are in center-size notation
                    x = (i + 0.5) / conv_w
                    y = (j + 0.5) / conv_h
                    
                    for scale, ars in zip(self.scales, self.aspect_ratios):
                        for ar in ars:
                            if not cfg.backbone.preapply_sqrt:
                                ar = sqrt(ar)

                            if cfg.backbone.use_pixel_scales:
                                w = scale * ar / cfg.input_w
                                h = scale / ar / cfg.input_h
                            else:
                                w = scale * ar / conv_w
                                h = scale / ar / conv_h
                            
                            # This is for backward compatability with a bug where I made everything square by accident
                            if cfg.backbone.use_square_anchors:
                                h = w

                            prior_data += [x, y, w, h]
                
                self.priors = torch.Tensor(prior_data).view(-1, 4)
                self.last_conv_size = (conv_w, conv_h)
        
        return self.priors

class FPN(ScriptModuleWrapper):
    """
    Implements a general version of the FPN introduced in
    https://arxiv.org/pdf/1612.03144.pdf

    Parameters (in cfg.fpn):
        - num_features (int): The number of output features in the fpn layers.
        - interpolation_mode (str): The mode to pass to F.interpolate.
        - num_downsample (int): The number of downsampled layers to add onto the selected layers.
                                These extra layers are downsampled from the last selected layer.

    Args:
        - in_channels (list): For each conv layer you supply in the forward pass,
                              how many features will it have?
    """
    __constants__ = ['interpolation_mode', 'num_downsample', 'use_conv_downsample',
                     'lat_layers', 'pred_layers', 'downsample_layers']

    def __init__(self, in_channels):
        super().__init__()

        self.lat_layers  = nn.ModuleList([
            nn.Conv2d(x, cfg.fpn.num_features, kernel_size=1)
            for x in reversed(in_channels)
        ])

        # This is here for backwards compatability
        padding = 1 if cfg.fpn.pad else 0
        self.pred_layers = nn.ModuleList([
            nn.Conv2d(cfg.fpn.num_features, cfg.fpn.num_features, kernel_size=3, padding=padding)
            for _ in in_channels
        ])

        if cfg.fpn.use_conv_downsample:
            self.downsample_layers = nn.ModuleList([
                nn.Conv2d(cfg.fpn.num_features, cfg.fpn.num_features, kernel_size=3, padding=1, stride=2)
                for _ in range(cfg.fpn.num_downsample)
            ])
        
        self.interpolation_mode  = cfg.fpn.interpolation_mode
        self.num_downsample      = cfg.fpn.num_downsample
        self.use_conv_downsample = cfg.fpn.use_conv_downsample

    @script_method_wrapper
    def forward(self, convouts:List[torch.Tensor]):
        """
        Args:
            - convouts (list): A list of convouts for the corresponding layers in in_channels.
        Returns:
            - A list of FPN convouts in the same order as x with extra downsample layers if requested.
        """

        out = []
        x = torch.zeros(1, device=convouts[0].device)
        for i in range(len(convouts)):
            out.append(x)

        # For backward compatability, the conv layers are stored in reverse but the input and output is
        # given in the correct order. Thus, use j=-i-1 for the input and output and i for the conv layers.
        j = len(convouts)
        for lat_layer in self.lat_layers:
            j -= 1

            if j < len(convouts) - 1:
                _, _, h, w = convouts[j].size()
                x = F.interpolate(x, size=(h, w), mode=self.interpolation_mode, align_corners=False)
            
            x = x + lat_layer(convouts[j])
            out[j] = x
        
        # This janky second loop is here because TorchScript.
        j = len(convouts)
        for pred_layer in self.pred_layers:
            j -= 1
            out[j] = F.relu(pred_layer(out[j]))

        # In the original paper, this takes care of P6
        if self.use_conv_downsample:
            for downsample_layer in self.downsample_layers:
                out.append(downsample_layer(out[-1]))
        else:
            for idx in range(self.num_downsample):
                # Note: this is an untested alternative to out.append(out[-1][:, :, ::2, ::2]). Thanks TorchScript.
                out.append(nn.functional.max_pool2d(out[-1], 1, stride=2))

        return out



class FusionModule(nn.Module): 
    def __init__(self, in_channels,  fpn_levels, out_dim):
        super().__init__()

        # ---Panoptic FPN---
        self.layer_modules = nn.ModuleList([])
        for i in range(fpn_levels):
            layer_module, _ = make_net(in_channels, [(out_dim, 3, {'padding': 1}), (None, -2, {})] * i + [(out_dim, 3, {'padding': 1})])
            self.layer_modules.append(layer_module)
        
        self.fusion_subnet, _ =  make_net(in_channels, [(out_dim*2, 3, {'padding': 1})] + [(out_dim, 3, {'padding': 1})] * 2 )

        self.upsample_layer = None
    def forward(self, fpn_outs):
        if self.upsample_layer is None:
            final_size = (fpn_outs[0].size(2), fpn_outs[0].size(3))
            self.upsample_layer = InterpolateModule(size=final_size, mode='bilinear', align_corners=False)

        fusion_maps = []
        for i in range(len(fpn_outs)):
            out = self.layer_modules[i](fpn_outs[i])
            out = self.upsample_layer(out)

            
            fusion_maps.append(out)
        
        fusion_out = sum(fusion_maps)  

        return fusion_out

class AddCoords(nn.Module):
    def __init__(self, with_r=False, use_cuda=True):
        super(AddCoords, self).__init__()
        self.with_r = with_r
        self.use_cuda = use_cuda

    def forward(self, input_tensor):
        """
        :param input_tensor: shape (N, C_in, H, W)
        :return:
        """
        batch_size_shape, channel_in_shape, dim_y, dim_x = input_tensor.shape
        xx_ones = torch.ones([1, 1, 1, dim_x], dtype=torch.int32)
        yy_ones = torch.ones([1, 1, 1, dim_y], dtype=torch.int32)

        xx_range = torch.arange(dim_y, dtype=torch.int32)
        yy_range = torch.arange(dim_x, dtype=torch.int32)
        xx_range = xx_range[None, None, :, None]
        yy_range = yy_range[None, None, :, None]

        xx_channel = torch.matmul(xx_range.float(), xx_ones.float())
        yy_channel = torch.matmul(yy_range.float(), yy_ones.float())

        # transpose y
        yy_channel = yy_channel.permute(0, 1, 3, 2)

        xx_channel = xx_channel.float() / (dim_y - 1)
        yy_channel = yy_channel.float() / (dim_x - 1)

        xx_channel = xx_channel * 2 - 1
        yy_channel = yy_channel * 2 - 1

        xx_channel = xx_channel.repeat(batch_size_shape, 1, 1, 1)
        yy_channel = yy_channel.repeat(batch_size_shape, 1, 1, 1)

        if torch.cuda.is_available and self.use_cuda:
            input_tensor = input_tensor.cuda()
            xx_channel = xx_channel.cuda()
            yy_channel = yy_channel.cuda()
        out = torch.cat([input_tensor, xx_channel, yy_channel], dim=1)

        if self.with_r:
            rr = torch.sqrt(torch.pow(xx_channel - 0.5, 2) + torch.pow(yy_channel - 0.5, 2))
            out = torch.cat([out, rr], dim=1)

        return out

class CAModule(nn.Module):  # Cross Attention Layer
    # Ref from SAGAN
    def __init__(self, in_dim , share_conv=False):
        super(CAModule, self).__init__()
        self.chanel_in = in_dim
        self.share_conv = share_conv
        self.query_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim//2, kernel_size=1)
        if share_conv is True:
            self.key_conv = self.query_conv
        else: 
            self.key_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim//2, kernel_size=1)
            self.value_conv = nn.Conv2d(in_channels=in_dim, out_channels=in_dim//2, kernel_size=1)
            self.out_conv = nn.Conv2d(in_channels=in_dim//2, out_channels=in_dim, kernel_size=1)

        self.softmax = nn.Softmax(dim=-1)
    def forward(self, x_query, x_key, query_type=None):
        """
            inputs :
                x : input feature maps( B X C X H X W)
            returns :
                out : attention value + input feature
                attention: B X (HxW) X (HxW)
        """
        m_batchsize, C, h_query, w_query = x_query.size()
        m_batchsize, C, h_key, w_key = x_key.size()

        proj_query = self.query_conv(x_query).view(m_batchsize, -1, w_query*h_query).permute(0, 2, 1)
        proj_key = self.key_conv(x_key).view(m_batchsize, -1, w_key*h_key)
        energy = torch.bmm(proj_query, proj_key)
        attention = self.softmax(energy)
        proj_value = self.value_conv(x_key).view(m_batchsize, -1, w_key*h_key) if self.share_conv is not True else x_key.view(m_batchsize, -1, w_key*h_key)

        out = torch.bmm(proj_value, attention.permute(0, 2, 1))
        out = out.view(m_batchsize, -1, h_query, w_query)
        out = self.out_conv(out)
        out = out + x_query

        return out


class EPSNet(nn.Module):
    """
    You can set the arguments by chainging them in the backbone config object in config.py.

    Parameters (in cfg.backbone):
        - selected_layers: The indices of the conv layers to use for prediction.
        - pred_scales:     A list with len(selected_layers) containing tuples of scales (see PredictionModule)
        - pred_aspect_ratios: A list of lists of aspect ratios with len(selected_layers) (see PredictionModule)
    """

    def __init__(self):
        super().__init__()

        # print("backbone: ", cfg.backbone)
        self.backbone = construct_backbone(cfg.backbone)

        # print("freeze_bn: ", cfg.freeze_bn) #freeze batch normalization?
        if cfg.freeze_bn:
            self.freeze_bn()
        

        #Fusion FPN
        self.fusion_layers = cfg.fusion_layers
        self.fusion_dim = cfg.fusion_dim


        # Compute mask_dim here and add it back to the config. 
        if cfg.mask_type == mask_type.direct: #???
            cfg.mask_dim = cfg.mask_size**2
        elif cfg.mask_type == mask_type.lincomb: #enter
            if cfg.mask_proto_use_grid: #false
                self.grid = torch.Tensor(np.load(cfg.mask_proto_grid_file)) #data/grid.npy???
                self.num_grids = self.grid.size(0)
            else: #enter
                self.num_grids = 0

            self.proto_src = cfg.mask_proto_src
            
            if self.proto_src is None: in_channels = 3
            elif cfg.fpn is not None: in_channels = cfg.fpn.num_features
            else: in_channels = self.backbone.channels[self.proto_src]
            in_channels += self.num_grids

            # The include_last_relu=false here is because we might want to change it to another function
            
            if cfg.proto_coordconv:
                in_channels += 2
            elif cfg.fpn_fusion:
                in_channels = self.fusion_dim
            
            self.proto_net, cfg.mask_dim = make_net(in_channels, cfg.mask_proto_net, include_last_relu=False)

            if cfg.mask_proto_bias:
                cfg.mask_dim += 1

        
        self.selected_layers = cfg.backbone.selected_layers
        src_channels = self.backbone.channels
        if cfg.fpn is not None:
            # Some hacky rewiring to accomodate the FPN
            self.fpn = FPN([src_channels[i] for i in self.selected_layers])
            self.selected_layers = list(range(len(self.selected_layers) + cfg.fpn.num_downsample))
            src_channels = [cfg.fpn.num_features] * len(self.selected_layers)


        if cfg.fpn_fusion is True:
            self.fusion_module =  FusionModule(src_channels[0], self.fusion_layers, out_dim=self.fusion_dim)  


        if cfg.ins_coordconv or cfg.sem_coordconv or cfg.proto_coordconv:
            self.addcoords = AddCoords()
        
        self.prediction_layers = nn.ModuleList()

        for idx, layer_idx in enumerate(self.selected_layers):
            # If we're sharing prediction module weights, have every module's parent be the first one
            parent = None
            if cfg.share_prediction_module and idx > 0:
                parent = self.prediction_layers[0]

            pred_in_ch = src_channels[layer_idx] + 2 if cfg.ins_coordconv else src_channels[layer_idx]
            pred = PredictionModule(pred_in_ch, src_channels[layer_idx],
                                    aspect_ratios = cfg.backbone.pred_aspect_ratios[idx],
                                    scales        = cfg.backbone.pred_scales[idx],
                                    parent        = parent)
            self.prediction_layers.append(pred)

        # Extra parameters for the extra losses
        if cfg.use_class_existence_loss:
            # This comes from the smallest layer selected
            # Also note that cfg.num_classes includes background
            self.class_existence_fc = nn.Linear(src_channels[-1], cfg.num_classes - 1)
    
        if cfg.cross_attention_fusion:
            self.CALayer = CAModule(src_channels[0], share_conv=False)


        if cfg.use_semantic_segmentation_loss:
            sem_in_ch = None
            if cfg.sem_src_fusion is True:
                sem_in_ch = self.fusion_dim
            elif cfg.sem_lincomb is True:
                sem_in_ch = src_channels[0]
            else: # normal semantic segmentation head
                sem_in_ch = src_channels[-1]

            if cfg.sem_coordconv:
                sem_in_ch += 2

            # Panoptic FPN Fusion Version
            if cfg.sem_src_fusion is True:
                self.semantic_seg_conv = nn.Sequential(
                    nn.Conv2d(sem_in_ch, cfg.stuff_num_classes, kernel_size=(1, 1))
                )
            
            elif cfg.sem_lincomb is True:
                self.semantic_seg_conv = nn.Sequential(
                    nn.Conv2d(sem_in_ch, 256, kernel_size=3),
                    # nn.BatchNorm2d(256),
                    nn.GroupNorm(32, 256),
                    nn.ReLU(True),
                    nn.Conv2d(256, (cfg.stuff_num_classes)*cfg.mask_dim, kernel_size=1),
                    nn.Tanh()
                )
            else: 
                self.semantic_seg_conv = nn.Sequential(
                    nn.Conv2d(sem_in_ch, cfg.stuff_num_classes, kernel_size=(1, 1))
                )
            
        # For use in evaluation
        self.detect = Detect(cfg.num_classes, bkg_label=0, top_k=200, conf_thresh=0.05, nms_thresh=0.5)

    
    def save_weights(self, path):
        """ Saves the model's weights using compression because the file sizes were getting too big. """
        torch.save(self.state_dict(), path)
    
    def load_weights(self, path):
        """ Loads weights from a compressed save file. """
        state_dict = torch.load(path)

        # For backward compatability, remove these (the new variable is called layers)
        for key in list(state_dict.keys()):
            if key.startswith('backbone.layer') and not key.startswith('backbone.layers'):
                del state_dict[key]
        
            # Also for backward compatibility with v1.0 weights, do this check
            if key.startswith('fpn.downsample_layers.'):
                if cfg.fpn is not None and int(key.split('.')[2]) >= cfg.fpn.num_downsample:
                    del state_dict[key]

        self.load_state_dict(state_dict)

    def init_weights(self, backbone_path):
        """ Initialize weights for training. """
        # Initialize the backbone with the pretrained weights.
        self.backbone.init_backbone(backbone_path)

        # Initialize the rest of the conv layers with xavier
        for name, module in self.named_modules():
            if isinstance(module, nn.Conv2d) and module not in self.backbone.backbone_modules:
                nn.init.xavier_uniform_(module.weight.data)
                
                if module.bias is not None:
                    if cfg.use_focal_loss and 'conf_layer' in name:
                        if not cfg.use_sigmoid_focal_loss:
                            # Initialize the last layer as in the focal loss paper.
                            # Because we use softmax and not sigmoid, I had to derive an alternate expression
                            # on a notecard. Define pi to be the probability of outputting a foreground detection.
                            # Then let z = sum(exp(x)) - exp(x_0). Finally let c be the number of foreground classes.
                            # Chugging through the math, this gives us
                            #   x_0 = log(z * (1 - pi) / pi)    where 0 is the background class
                            #   x_i = log(z / c)                for all i > 0
                            # For simplicity (and because we have a degree of freedom here), set z = 1. Then we have
                            #   x_0 =  log((1 - pi) / pi)       note: don't split up the log for numerical stability
                            #   x_i = -log(c)                   for all i > 0
                            module.bias.data[0]  = np.log((1 - cfg.focal_loss_init_pi) / cfg.focal_loss_init_pi)
                            module.bias.data[1:] = -np.log(module.bias.size(0) - 1)
                        else:
                            module.bias.data[0]  = -np.log(cfg.focal_loss_init_pi / (1 - cfg.focal_loss_init_pi))
                            module.bias.data[1:] = -np.log((1 - cfg.focal_loss_init_pi) / cfg.focal_loss_init_pi)
                    else:
                        module.bias.data.zero_()

    def train(self, mode=True):
        super().train(mode)

        if cfg.freeze_bn:
            self.freeze_bn()

    def freeze_bn(self):
        """ Adapted from https://discuss.pytorch.org/t/how-to-train-with-frozen-batchnorm/12106/8 """
        for module in self.modules():
            if isinstance(module, nn.BatchNorm2d):
                module.eval()

                module.weight.requires_grad = False
                module.bias.requires_grad = False

    def forward(self, x):
        """ The input should be of size [batch_size, 3, img_h, img_w] """

        
        # plt.imshow(x.permute(0,2,3,1)[0,:,:,:].detach().cpu().numpy())
        # plt.savefig('visual_test/input.png')
        # plt.cla()


        with timer.env('backbone'):
            outs = self.backbone(x)

        if cfg.fpn is not None:
            with timer.env('fpn'):
                # Use backbone.selected_layers because we overwrote self.selected_layers
                outs = [outs[i] for i in cfg.backbone.selected_layers]
                outs = self.fpn(outs)
        
        proto_out = None
        if cfg.fpn_fusion is True:
            fusion_maps = self.fusion_module(outs[:self.fusion_layers])   # fusion all levels feature map from map into single one  

        if cfg.mask_type == mask_type.lincomb and cfg.eval_mask_branch:
            with timer.env('proto'):
                proto_x = x if self.proto_src is None else outs[self.proto_src]

                # FPN Fusion
                if cfg.proto_src_fusion is True:
                    proto_x = fusion_maps
                
                if cfg.cross_attention_fusion is True:
                    P_query = outs[0]
                    proto_x = P_query
                    
                    for layer in range(self.fusion_layers):
                        z = self.CALayer(x_query=P_query, x_key=outs[layer]) - P_query
                        proto_x = proto_x + z

                if self.num_grids > 0:
                    grids = self.grid.repeat(proto_x.size(0), 1, 1, 1)
                    proto_x = torch.cat([proto_x, grids], dim=1)
                    
                if cfg.proto_coordconv:
                    proto_x = self.addcoords(proto_x)

                proto_out = self.proto_net(proto_x)
                proto_out = cfg.mask_proto_prototype_activation(proto_out)

                if cfg.mask_proto_prototypes_as_features:
                    # Clone here because we don't want to permute this, though idk if contiguous makes this unnecessary
                    proto_downsampled = proto_out.clone()

                    if cfg.mask_proto_prototypes_as_features_no_grad:
                        proto_downsampled = proto_out.detach()
                
                # Move the features last so the multiplication is easy
                proto_out = proto_out.permute(0, 2, 3, 1).contiguous()

                if cfg.mask_proto_bias:
                    bias_shape = [x for x in proto_out.size()]
                    bias_shape[-1] = 1
                    proto_out = torch.cat([proto_out, torch.ones(*bias_shape)], -1)


        with timer.env('pred_heads'):
            pred_outs = { 'loc': [], 'conf': [], 'mask': [], 'priors': [] }

            if cfg.use_instance_coeff:
                pred_outs['inst'] = []
            
            for idx, pred_layer in zip(self.selected_layers, self.prediction_layers):
                pred_x = outs[idx]

                if cfg.mask_type == mask_type.lincomb and cfg.mask_proto_prototypes_as_features:
                    # Scale the prototypes down to the current prediction layer's size and add it as inputs
                    proto_downsampled = F.interpolate(proto_downsampled, size=outs[idx].size()[2:], mode='bilinear', align_corners=False)
                    pred_x = torch.cat([pred_x, proto_downsampled], dim=1)

                # A hack for the way dataparallel works
                if cfg.share_prediction_module and pred_layer is not self.prediction_layers[0]:
                    pred_layer.parent = [self.prediction_layers[0]]

                if cfg.ins_coordconv:
                    pred_x = self.addcoords(pred_x)
                    
                p = pred_layer(pred_x)
                
                for k, v in p.items():
                    pred_outs[k].append(v)
            # print("mask ", (pred_outs['mask'][0]).size())

        # ===revised=== 
        num_priors = []
        for k, v in pred_outs.items():
            if k == 'loc':
                for _v in v:
                    num_priors.append(_v.size(1))
            pred_outs[k] = torch.cat(v, -2)
        pred_outs['layer'] = num_priors


        if proto_out is not None:
            pred_outs['proto'] = proto_out
        
        if self.training:

            # For the extra loss functions
            if cfg.use_class_existence_loss:
                pred_outs['classes'] = self.class_existence_fc(outs[-1].mean(dim=(2, 3)))

            with timer.env('segm'):
                if cfg.use_semantic_segmentation_loss:
                    sem_in = None
                    if cfg.sem_src_fusion is True:
                        sem_in = fusion_maps
                    elif cfg.sem_lincomb is True:
                        sem_in = outs[-1] 

                    if cfg.sem_coordconv:
                        sem_in = self.addcoords(sem_in)       

                    pred_outs['segm'] = self.semantic_seg_conv(sem_in)
                        # pred_outs['segm'] = self.semantic_seg_conv(outs[-1]) #lincomb version
          
            return pred_outs
        else:
            if cfg.use_sigmoid_focal_loss:
                # Note: even though conf[0] exists, this mode doesn't train it so don't use it
                pred_outs['conf'] = torch.sigmoid(pred_outs['conf'])
            elif cfg.use_objectness_score:
                # See focal_loss_sigmoid in multibox_loss.py for details
                objectness = torch.sigmoid(pred_outs['conf'][:, :, 0])
                pred_outs['conf'][:, :, 1:] = objectness[:, :, None] * F.softmax(pred_outs['conf'][:, :, 1:], -1)
                pred_outs['conf'][:, :, 0 ] = 1 - objectness
            else:
                pred_outs['conf'] = F.softmax(pred_outs['conf'], -1)

            if cfg.use_sem_output is True:
                sem_in = None
                if cfg.sem_src_fusion is True:
                    sem_in = fusion_maps
                elif cfg.sem_lincomb is True:
                    sem_in = outs[-1] 

                if  cfg.sem_coordconv:
                    sem_in = self.addcoords(sem_in)       
                                
                pred_outs['segm'] = self.semantic_seg_conv(sem_in)
                
            return self.detect(pred_outs)






# Some testing code
if __name__ == '__main__':
    from utils.functions import init_console
    init_console()

    # Use the first argument to set the config if you want
    import sys
    if len(sys.argv) > 1:
        from data.config import set_cfg
        set_cfg(sys.argv[1])

    net = EPSNet()

    net.train()
    net.init_weights(backbone_path='weights/' + cfg.backbone.path)

    # GPU
    net = net.cuda()
    
    cudnn.benchmark = True
    torch.set_default_tensor_type('torch.cuda.FloatTensor')

    x = torch.zeros((1, 3, 550, 550))
   

    print()
    y = net(x)
    for k, a in y.items():
        print(k + ': ', a.size(), torch.sum(a))

    net(x)
    # exit()
    avg = MovingAverage()
    try:
        while True:
            timer.reset()
            with timer.env('everything else'):
                net(x)
            avg.add(timer.total_time())
            # print('\033[2J') # Moves console cursor to 0,0
            timer.print_stats()
            # print('Avg fps: %.2f\tAvg ms: %.2f         ' % (1/avg.get_avg(), avg.get_avg()*1000))
    except KeyboardInterrupt:
        pass
