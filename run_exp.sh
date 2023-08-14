#! /bin/bash

####
# best: batch size 16, lr 1e-4, c16, l1 loss
# python train_igp.py --exp_name unet_c32 --options net.c0=32
# python train_igp.py --exp_name unet_c16 --options net.c0=16
# python train_igp.py --exp_name unet_16_lr1e4_ep100 --options net.c0=16,train.lr=1e-4,train.epoch_num=100
# python train_igp.py --exp_name unet_16_lr1e5_ep100 --options net.c0=16,train.lr=1e-5,train.epoch_num=100
# python train_igp.py --exp_name unet_c16_lr1e4_B32 --options net.c0=16,train.batch_size=32,train.lr=1e-4

# python train_igp.py --exp_name unet_c8_lr1e4 --options net.c0=8,train.lr=1e-4

python train_igp.py --exp_name unet_c16_is10 --options net.c0=16,net.i_s_weight=10
python train_igp.py --exp_name unet_c16_is20 --options net.c0=16,net.i_s_weight=20

python train_igp.py --exp_name resnet_c16_l4 --options net.c0=16,net.backbone=resnet,net.resnet_depth=4
python train_igp.py --exp_name resnet_c8_l4 --options net.c0=8,net.backbone=resnet,net.resnet_depth=4
python train_igp.py --exp_name resnet_c8_l5 --options net.c0=8,net.backbone=resnet,net.resnet_depth=5
