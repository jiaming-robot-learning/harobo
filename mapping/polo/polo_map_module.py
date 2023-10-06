# Adapted from https://github.com/facebookresearch/home-robot


from typing import Tuple, Optional, Dict, List
from torch.nn.utils.rnn import pad_sequence
import cv2
import matplotlib.pyplot as plt
import numpy as np
import skimage.morphology
import torch
import torch.nn as nn
import trimesh.transformations as tra
from torch import IntTensor, Tensor
from torch.nn import functional as F

import home_robot.mapping.map_utils as mu
from utils import depth as du
import home_robot.utils.pose as pu
import home_robot.utils.rotation as ru
from mapping.polo.constants import MapConstants as MC
from utils.visualization import (
    display_grayscale,
    display_rgb,
    plot_image,
    save_image, 
    draw_top_down_map, 
    Recording, 
    visualize_gt,
    render_plt_image,
    visualize_pred,
    show_points, 
    show_voxel_with_prob, 
    show_voxel_with_logit,
    save_img_tensor)


# For debugging input and output maps - shows matplotlib visuals
debug_maps = False
    

class POLoMapModule(nn.Module):
    """
    This class is responsible for updating a dense 2D semantic map with one channel
    per object category, the local and global maps and poses, and generating
    map features — it is a stateless PyTorch module with no trainable parameters.

    Map proposed in:
    Object Goal Navigation using Goal-Oriented Semantic Exploration
    https://arxiv.org/pdf/2007.00643.pdf
    https://github.com/devendrachaplot/Object-Goal-Navigation
    """

    # If true, display point cloud visualizations using Open3d
    debug_mode = False

    def __init__(
        self,
        # config,
        frame_height: int,
        frame_width: int,
        camera_height: int,
        hfov: int,
        num_sem_categories: int,
        map_size_cm: int,
        map_resolution: int,
        vision_range: int,
        explored_radius: int,
        been_close_to_radius: int,
        global_downscaling: int,
        du_scale: int,
        cat_pred_threshold: float,
        exp_pred_threshold: float,
        map_pred_threshold: float,
        min_depth: float = 0.2,
        max_depth: float = 5.0,
        must_explore_close: bool = False,
        min_obs_height_cm: int = 0,
        dilate_obstacles: bool = True,
        dilate_iter: int = 1,
        dilate_size: int = 3,
        probabilistic: bool = False,
        probability_prior: float = 0.2,
        close_range: int = 150, # 1.5m
        confident_threshold: float = 0.7,
    ):
        """
        Arguments:
            frame_height: first-person frame height
            frame_width: first-person frame width
            camera_height: camera sensor height (in metres)
            hfov: horizontal field of view (in degrees)
            num_sem_categories: number of semantic segmentation categories
            map_size_cm: global map size (in centimetres)
            map_resolution: size of map bins (in centimeters)
            vision_range: diameter of the circular region of the local map
             that is visible by the agent located in its center (unit is
             the number of local map cells)
            explored_radius: radius (in centimeters) of region of the visual cone
             that will be marked as explored
            been_close_to_radius: radius (in centimeters) of been close to region
            global_downscaling: ratio of global over local map
            du_scale: frame downscaling before projecting to point cloud
            cat_pred_threshold: number of depth points to be in bin to
             classify it as a certain semantic category
            exp_pred_threshold: number of depth points to be in bin to
             consider it as explored
            map_pred_threshold: number of depth points to be in bin to
             consider it as obstacle
            must_explore_close: reduce the distance we need to get to things to make them work
            min_obs_height_cm: minimum height of obstacles (in centimetres)
        """
        super().__init__()

        self.screen_h = frame_height
        self.screen_w = frame_width
        self.camera_matrix = du.get_camera_matrix(self.screen_w, self.screen_h, hfov)
        self.num_sem_categories = num_sem_categories
        self.must_explore_close = must_explore_close

        self.map_size_parameters = mu.MapSizeParameters(
            map_resolution, map_size_cm, global_downscaling
        )
        self.resolution = map_resolution
        self.global_map_size_cm = map_size_cm
        self.global_downscaling = global_downscaling
        self.local_map_size_cm = self.global_map_size_cm // self.global_downscaling
        self.global_map_size = self.global_map_size_cm // self.resolution
        self.local_map_size = self.local_map_size_cm // self.resolution
        self.xy_resolution = self.z_resolution = map_resolution
        self.vision_range = vision_range
        self.explored_radius = explored_radius
        self.been_close_to_radius = been_close_to_radius
        self.du_scale = du_scale
        self.cat_pred_threshold = cat_pred_threshold
        self.exp_pred_threshold = exp_pred_threshold
        self.map_pred_threshold = map_pred_threshold

        self.max_depth = max_depth * 100.0
        self.min_depth = min_depth * 100.0
        self.agent_height = camera_height * 100.0
        self.max_voxel_height = int(360 / self.z_resolution)
        self.min_voxel_height = int(-40 / self.z_resolution)
        self.min_obs_height_cm = min_obs_height_cm
        self.min_mapped_height = int(
            self.min_obs_height_cm / self.z_resolution - self.min_voxel_height
        )
        # ignore the ground
        self.filtered_min_height = int(
            20 / self.z_resolution - self.min_voxel_height
        )  # 20cm
        self.max_mapped_height = int(
            (self.agent_height + 1) / self.z_resolution - self.min_voxel_height
        )
        self.shift_loc = [self.vision_range * self.xy_resolution // 2, 0, np.pi / 2.0]

        # For cleaning up maps
        self.dilate_obstacles = dilate_obstacles
        # self.dilate_kernel = np.ones((dilate_size, dilate_size))
        self.dilate_size = dilate_size
        # self.dilate_iter = dilate_iter
        
        self.probabilistic = probabilistic
        # For probabilistic map updates
        self.dist_rows = torch.arange(1, self.vision_range + 1).float()
        self.dist_rows = self.dist_rows.unsqueeze(1).repeat(1, self.vision_range)
        self.dist_cols = torch.arange(1, self.vision_range + 1).float() - (self.vision_range / 2)
        self.dist_cols = torch.abs(self.dist_cols)
        self.dist_cols = self.dist_cols.unsqueeze(0).repeat(self.vision_range, 1)

        self.close_range = close_range // self.xy_resolution # 150 cm
        self.confident_threshold = confident_threshold # above which considered a hard detection
        self.confirm_detection_threashold = 0.5 # above which considered a detection
        
        self.prior_logit = torch.logit(torch.tensor(probability_prior)) # prior probability of objects
        self.vr_matrix = torch.zeros((1, self.vision_range, self.vision_range))
        self.prior_matrix = torch.full((1, self.vision_range, self.vision_range), self.prior_logit)

        self.dialate_kernel = torch.ones((1, 1, dilate_size, dilate_size), dtype=torch.float32)

    @torch.no_grad()
    def forward(
        self,
        obs: Tensor,
        pose_delta: Tensor,
        camera_pose: Tensor,
        prev_map: Tensor,
        prev_pose: Tensor,
        detection_results: Optional[List[Dict[str, Tensor]]] = None,
        lmb: Tensor = None,
    ) -> Tuple[Tensor, Tensor, Tensor, Tensor, Tensor, IntTensor, Tensor]:
  
        """Update local map and sensor pose given a new observation using parameter-free
        differentiable projective geometry.

        Args:
            obs: current frame containing (rgb, depth, segmentation) of shape
             (batch_size, 3 + 1 + num_sem_categories, frame_height, frame_width)
            pose_delta: delta in pose since last frame of shape (batch_size, 3)
            prev_map: previous local map of shape
             (batch_size, MC.NON_SEM_CHANNELS + num_sem_categories, M, M)
            prev_pose: previous pose of shape (batch_size, 3)
            camera_pose: current camera poseof shape (batch_size, 4, 4)

        Returns:
            current_map: current local map updated with current observation
             and location of shape (batch_size, MC.NON_SEM_CHANNELS + num_sem_categories, M, M)
            current_pose: current pose updated with pose delta of shape (batch_size, 3)
        """
        batch_size, obs_channels, h, w = obs.size()
        device, dtype = obs.device, obs.dtype
        if camera_pose is not None:
            # TODO: make consistent between sim and real
            # hab_angles = pt.matrix_to_euler_angles(camera_pose[:, :3, :3], convention="YZX")
            # angles = pt.matrix_to_euler_angles(camera_pose[:, :3, :3], convention="ZYX")
            angles = torch.Tensor(
                [tra.euler_from_matrix(p[:3, :3].cpu(), "rzyx") for p in camera_pose]
            )
            # For habitat - pull x angle
            # tilt = angles[:, -1]
            # For real robot
            tilt = angles[:, 1]

            # Get the agent pose
            # hab_agent_height = camera_pose[:, 1, 3] * 100
            agent_pos = camera_pose[:, :3, 3] * 100
            agent_height = agent_pos[:, 2]
        else:
            tilt = torch.zeros(batch_size)
            agent_height = self.agent_height

        depth = obs[:, 3, :, :].float()
        depth[depth > self.max_depth] = 0
        point_cloud_t = du.get_point_cloud_from_z_t(
            depth, self.camera_matrix, device, scale=self.du_scale
        )

        if self.debug_mode:
            from home_robot.utils.point_cloud import show_point_cloud

            rgb = obs[:, :3, :: self.du_scale, :: self.du_scale].permute(0, 2, 3, 1)
            xyz = point_cloud_t[0].reshape(-1, 3)
            rgb = rgb[0].reshape(-1, 3)
            print("-> Showing point cloud in camera coords")
            show_point_cloud(
                (xyz / 100.0).cpu().numpy(),
                (rgb / 255.0).cpu().numpy(),
                orig=np.zeros(3),
            )

        point_cloud_base_coords = du.transform_camera_view_t(
            point_cloud_t, agent_height, torch.rad2deg(tilt).cpu().numpy(), device
        )

        # Show the point cloud in base coordinates for debugging
        if self.debug_mode:
            print()
            print("------------------------------")
            print("agent angles =", angles)
            print("agent tilt   =", tilt)
            print("agent height =", agent_height, "preset =", self.agent_height)
            xyz = point_cloud_base_coords[0].reshape(-1, 3)
            print("-> Showing point cloud in base coords")
            show_point_cloud(
                (xyz / 100.0).cpu().numpy(),
                (rgb / 255.0).cpu().numpy(),
                orig=np.zeros(3),
            )

        #################### prob features ####################
        
        # we use the detection score as feat for point cloud
        # we assume total_num_instance is the same for all batch (padded with 0)

        # first take max prob value for pixel
        prob_feat = torch.zeros(batch_size, 1, h // self.du_scale * w // self.du_scale).to(device)
        if detection_results is not None:
            scores = detection_results["scores"] # [B, total_num_instance]
            classes = detection_results["classes"] # [B, total_num_instance]
            masks = detection_results["masks"].float() # [B, total_num_instance, H, W]
            relevance = detection_results["relevance"] #torch.tensor([0, 1, 0.7 , 0, 0]).to(device)

            num_detected_instance = 0
            if masks.shape[1] != 0: # no instance detected
                score_relevence = scores * relevance[classes] # [B, total_num_instance]
                prob_feat = torch.einsum('bnhw,bn->bnhw',masks, score_relevence) # [B, N, H, W]
                prob_feat,_ = torch.max(prob_feat, dim=1, keepdim=True) # [B,1, H, W]
                # we use maxpool2d instead of avgpool2d to preserve the prob value
                prob_feat = nn.MaxPool2d(self.du_scale)(prob_feat).view(
                        batch_size, 1,  h // self.du_scale * w // self.du_scale
                    ) # [B, 1,  H*W] after scaling

                # TODO: work with vectorized version later
                detect_idx = (scores[0] > self.confirm_detection_threashold) # [total_num_instance]
                num_detected_instance = detect_idx.sum().item()
                if num_detected_instance > 0:
                    detected_instances_classes = classes[0][detect_idx] # [num_detected_instance]
                    detected_instances_scores = scores[0][detect_idx] # [num_detected_instance]
                    detected_instances_masks = masks[0][detect_idx] # [num_detected_instance, H, W]
                    detected_instances_masks = detected_instances_masks.unsqueeze(0) # [1, num_detected_instance, H, W]
                    detected_instances_masks = nn.MaxPool2d(self.du_scale)(detected_instances_masks).view(
                        1, detected_instances_masks.shape[1],  h // self.du_scale * w // self.du_scale
                    ) # [1, num_detected_instance,  H*W] after scaling
            
        #################### prob features ####################

        point_cloud_map_coords = du.transform_pose_t(
            point_cloud_base_coords, self.shift_loc, device
        )

        if self.debug_mode:
            xyz = point_cloud_base_coords[0].reshape(-1, 3)
            print("-> Showing point cloud in map coords")
            show_point_cloud(
                (xyz / 100.0).cpu().numpy(),
                (rgb / 255.0).cpu().numpy(),
                orig=np.zeros(3),
            )

        # voxel_channels = 2 + self.num_sem_categories # first is for 3d structure, last is for prob feat
        
        # init_grid = torch.zeros(
        #     batch_size,
        #     voxel_channels,
        #     self.vision_range,
        #     self.vision_range,
        #     self.max_voxel_height - self.min_voxel_height,
        #     device=device,
        #     dtype=torch.float32,
        # )
        # feat = torch.ones(
        #     batch_size,
        #     voxel_channels-1, # cat + prob
        #     self.screen_h // self.du_scale * self.screen_w // self.du_scale,
        #     device=device,
        #     dtype=torch.float32,
        # )

        # feat[:, 1:, :] = nn.AvgPool2d(self.du_scale)(obs[:, 4:, :, :]).view(
        #     batch_size, obs_channels - 4, h // self.du_scale * w // self.du_scale
        # )

        # ------------more channels-------------
        feat_all_points_channel = 1
        feat_ob_channel = feat_all_points_channel + obs_channels - 4
        feat_prob_channel = feat_ob_channel + 1
        if num_detected_instance > 0:

            feat_instance_channel = feat_prob_channel + num_detected_instance
            voxel_channels = feat_instance_channel  # first is for 3d structure, last is for prob feat
        
        else:
            voxel_channels = feat_prob_channel # first is for 3d structure, last is for prob feat
            
        init_grid = torch.zeros(
            batch_size,
            voxel_channels,
            self.vision_range,
            self.vision_range,
            self.max_voxel_height - self.min_voxel_height,
            device=device,
            dtype=torch.float32,
        )
        feat = torch.ones(
            batch_size,
            voxel_channels, 
            self.screen_h // self.du_scale * self.screen_w // self.du_scale,
            device=device,
            dtype=torch.float32,
        )

        feat[:, 1:feat_ob_channel, :] = nn.AvgPool2d(self.du_scale)(obs[:, 4:, :, :]).view(
            batch_size, obs_channels - 4, h // self.du_scale * w // self.du_scale
        )
        feat[:, feat_ob_channel:feat_prob_channel, :] = prob_feat

        if num_detected_instance > 0:
            feat[:, feat_prob_channel:feat_instance_channel, :] = detected_instances_masks
                
            
        # total feat channel num = 
        #   feat_all_points_channel_num (all points) +
        #   feat_ob_channel_num (original feats) +
        #   feat_prob_channel_num (prob feat) +
        #   feat_instance_channel_num (instance feat)
        # ----------------------------
        
        XYZ_cm_std = point_cloud_map_coords.float()
        XYZ_cm_std[..., :2] = XYZ_cm_std[..., :2] / self.xy_resolution
        XYZ_cm_std[..., :2] = (
            (XYZ_cm_std[..., :2] - self.vision_range // 2.0) / self.vision_range * 2.0
        )
        XYZ_cm_std[..., 2] = XYZ_cm_std[..., 2] / self.z_resolution
        XYZ_cm_std[..., 2] = (
            (
                XYZ_cm_std[..., 2]
                - (self.max_voxel_height + self.min_voxel_height) // 2.0
            )
            / (self.max_voxel_height - self.min_voxel_height)
            * 2.0
        )
        XYZ_cm_std = XYZ_cm_std.permute(0, 3, 1, 2) # [B, 3, H, W]
        XYZ_cm_std = XYZ_cm_std.view(
            XYZ_cm_std.shape[0],
            XYZ_cm_std.shape[1],
            XYZ_cm_std.shape[2] * XYZ_cm_std.shape[3],
        ) # [B, 3, H*W]

        # voxels = du.splat_feat_nd_max(init_grid, feat, XYZ_cm_std).transpose(2, 3)
        voxels = du.splat_feat_nd(init_grid, feat, XYZ_cm_std).transpose(2, 3)

        all_height_proj = voxels[:,:1,...].sum(4)
        # ignore objects that are too low
        filtered_height_proj = voxels[...,
            self.filtered_min_height : self.max_mapped_height
        ].sum(4)
        # the agent_height range corresponds to 0cm to 120cm 
        agent_height_proj = voxels[:,:1,:,:,
            self.min_mapped_height : self.max_mapped_height
        ].sum(4)
     
        fp_map_pred = agent_height_proj[:, 0:1, :, :]
        fp_exp_pred = all_height_proj[:, 0:1, :, :]
        fp_map_pred = fp_map_pred / self.map_pred_threshold
        fp_exp_pred = fp_exp_pred / self.exp_pred_threshold

        close_exp = fp_exp_pred.clone() # [B, 1, H, W]
        close_exp[:,:, self.close_range:,:] = 0 # only consider the close range 1.5m
        
        ################ probabilitic ################
        
        prob_map, _ = voxels[:,feat_prob_channel-1,:,:,self.filtered_min_height : self.max_mapped_height].max(3)
        # prob_map = voxels[:,-1,:,:,self.filtered_min_height : self.max_mapped_height].sum(3)

        # TODO: should we use close_range or exp, or just all viewable area?
        # we can use a smaller prior for all viewable area, and bigger prior for close range
        prob_logit = torch.logit(prob_map,eps=1e-6) - self.prior_logit # 
        prob_logit[fp_exp_pred.squeeze(1) == 0] = 0 # set unviewable area to 0
        prob_logit = torch.clamp(prob_logit, min=-10, max=10)        

        

        ############### end probabilistic ###############


        agent_view = torch.zeros(
            batch_size,
            MC.NON_SEM_CHANNELS + self.num_sem_categories,
            self.local_map_size_cm // self.xy_resolution,
            self.local_map_size_cm // self.xy_resolution,
            device=device,
            dtype=dtype,
        )

        # Update agent view from the fp_map_pred
        if self.dilate_obstacles:
            
            fp_map_pred =  torch.nn.functional.conv2d(
                fp_map_pred, self.dialate_kernel.to(device), padding=self.dilate_size // 2
            ).clamp(0, 1)

        x1 = self.local_map_size_cm // (self.xy_resolution * 2) - self.vision_range // 2
        x2 = x1 + self.vision_range
        y1 = self.local_map_size_cm // (self.xy_resolution * 2)
        y2 = y1 + self.vision_range
        agent_view[:, MC.OBSTACLE_MAP : MC.OBSTACLE_MAP + 1, y1:y2, x1:x2] = fp_map_pred
        agent_view[:, MC.EXPLORED_MAP : MC.EXPLORED_MAP + 1, y1:y2, x1:x2] = fp_exp_pred
        agent_view[:, MC.BEEN_CLOSE_MAP : MC.BEEN_CLOSE_MAP + 1, y1:y2, x1:x2] = close_exp
        agent_view[:, MC.PROBABILITY_MAP , y1:y2, x1:x2] = prob_logit
        agent_view[:, MC.VOXEL_START: MC.NON_SEM_CHANNELS, y1:y2, x1:x2] = voxels[:,feat_prob_channel-1,:,:,
            : self.max_mapped_height
        ].permute(0,3,1,2) # [B, H, W, C] -> [B, C, H, W]
        
        # original feats
        agent_view[:, MC.NON_SEM_CHANNELS :, y1:y2, x1:x2] = (
            filtered_height_proj[:, 1:feat_ob_channel] / self.cat_pred_threshold
        )
        
        #### load channels for 3D occupancy and instance detection ####
        occupaid_voxel = torch.zeros(
            batch_size,
            self.max_mapped_height,
            self.local_map_size_cm // self.xy_resolution,
            self.local_map_size_cm // self.xy_resolution,
            device=device,
            dtype=dtype,
        )
            
        occupaid_voxel[..., y1:y2, x1:x2] = voxels[:,0,:,:, : self.max_mapped_height].permute(0,3,1,2)
        
        if num_detected_instance > 0:
            instances = torch.zeros(
                batch_size,
                num_detected_instance,
                self.local_map_size_cm // self.xy_resolution,
                self.local_map_size_cm // self.xy_resolution,
                device=device,
                dtype=dtype,
            )
            instances[..., y1:y2, x1:x2] = filtered_height_proj[:,feat_prob_channel:feat_instance_channel] # [B, N, H, W]
            
            agent_view = torch.cat([agent_view, occupaid_voxel, instances], dim=1)
        else:
            agent_view = torch.cat([agent_view, occupaid_voxel], dim=1)
        ####################
        
        current_pose = pu.get_new_pose_batch(prev_pose.clone(), pose_delta)
        st_pose = current_pose.clone().detach()

        st_pose[:, :2] = -(
            (
                st_pose[:, :2] * 100.0 / self.xy_resolution
                - self.local_map_size_cm // (self.xy_resolution * 2)
            )
            / (self.local_map_size_cm // (self.xy_resolution * 2))
        )
        st_pose[:, 2] = 90.0 - (st_pose[:, 2])

        rot_mat, trans_mat = ru.get_grid(st_pose, agent_view.size(), dtype)
        rotated = F.grid_sample(agent_view, rot_mat, align_corners=True)
        translated = F.grid_sample(rotated, trans_mat, align_corners=True)

        
        #### unload channels for 3D occupancy and instance detection ####
        ori_channel_num = MC.NON_SEM_CHANNELS + self.num_sem_categories
        occupaid_voxel_st = translated[:,ori_channel_num:ori_channel_num+self.max_mapped_height,:,:]
        if num_detected_instance > 0:
            instances_st = translated[:,ori_channel_num+self.max_mapped_height:,:,:] # [B, N, H, W]
        translated = translated[:,:ori_channel_num,:,:]
        ####################

        # --------------- process instances maps ----------------
        instances_dict = None
        if num_detected_instance > 0:
            
            # get bounding box for each instance
            # TODO: currently assumes batch size = 1
            instances_dict = {k:[] for k in range(self.num_sem_categories)}
            
            for i in range(num_detected_instance):
                ins_mask = instances_st[0,i,:,:].nonzero()
                if ins_mask.shape[0] > 0:

                    x1 = ins_mask[:,0].min().item()
                    x2 = ins_mask[:,0].max().item()
                    y1 = ins_mask[:,1].min().item()
                    y2 = ins_mask[:,1].max().item()
                    bb = np.array([x1, x2, y1, y2]) + lmb[0][[0,0,2,2]].cpu().numpy() # convert to global map coordinates 
                    center = np.array([(x1+x2)/2, (y1+y2)/2]) + lmb[0][[0,2]].cpu().numpy() # convert to global map coordinates

                    ins_obj = {
                        "bb": bb,
                        "center": center,
                        "score": detected_instances_scores[i].item(),
                        "class": detected_instances_classes[i].item(),
                    }             
                    instances_dict[detected_instances_classes[i].item()].append(ins_obj)

                    
        # --------------- end process instances maps ----------------
        
        
        # Clamp to [0, 1] after transform agent view to map coordinates
        idx_no_prob = list(range(0,MC.PROBABILITY_MAP)) + \
                    list(range(MC.NON_SEM_CHANNELS, MC.NON_SEM_CHANNELS+self.num_sem_categories))
        translated[:,idx_no_prob] = torch.clamp(translated[:,idx_no_prob], min=0.0, max=1.0)

        maps = torch.cat((prev_map.unsqueeze(1), translated.unsqueeze(1)), 1)
        current_map, _ = torch.max(
            maps[:, :, : MC.NON_SEM_CHANNELS + self.num_sem_categories], 1
        )

        ############### Bayesian update ###############
        current_map[:, MC.PROBABILITY_MAP, :, :] = torch.sum(maps[:, :, MC.PROBABILITY_MAP,:, :], 1)
        current_map[:, MC.PROBABILITY_MAP, :, :] = torch.clamp(current_map[:, MC.PROBABILITY_MAP, :, :], min=-10, max=10)
        goal_idx = MC.NON_SEM_CHANNELS + 1 # goal object

        # NOTE: here we mark all areas that have been close to the goal object as confident
        # we assume if we have been close to the object, we can identify the object, and update the semantic correctly
        # If we don't go to goal, then most likely the object is not the goal object on the specified receptacle
        
        # only for evaluation
        # checking_area = (translated[:, MC.BEEN_CLOSE_MAP] == 1) \
        #             & (current_map[:, MC.PROBABILITY_MAP] > self.prior_logit) \
        #             & (prev_map[:, MC.BEEN_CLOSE_MAP] != 1) # only check the area that we have not been close to, to avoid repeated checking
        # checking_area = checking_area.sum(dim=(1,2)).cpu().numpy() * self.resolution * self.resolution / 10000 # m^2
        
        # extras = {
        #     "checking_area": checking_area, # ndarray of size [B]
        # }

        # if been close to and the prob is very low
        # NOTE: however, this doesn't work well, because the prob can be hight for a recepticle
        # and make the agent repeatively checking the recepticle. We need to have different prob 
        # for different objects in order to make this work
        # So instead, we mark all close area to be low prob. Effectively, we only ask the agent to check
        # the area that has not been close to once, and then we can be confident about the object not being there
        # if the object detection model doesn't detect it
        # confident_no_obj = (current_map[:, MC.BEEN_CLOSE_MAP] == 1) \
        #                 & (current_map[:, MC.PROBABILITY_MAP] < self.prior_logit) 
        
        confident_no_obj = current_map[:, MC.BEEN_CLOSE_MAP] == 1

        current_map[:, MC.PROBABILITY_MAP][confident_no_obj] = -10 

        ############### end Bayesian update ###############
        
        ############## voxel update ################
        # we only update occupaid voxel (by current observation)
        # if voxel is empty in the previous map (isinf), then we assign the logit of the voxel: 
        # otherwise, update with l(p^t) = l(p^t-1) + l(p^t) - l(p)
        is_occupaid = occupaid_voxel_st >0.5
        is_pre_empty = prev_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:].isinf()
        need_assign_logit = is_occupaid & is_pre_empty
        need_addition_logit = is_occupaid & ~is_pre_empty
        voxel_logit = torch.logit(
            translated[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:],eps=1e-6)
        
        updated = prev_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:].clone()
        # case 1
        updated[need_assign_logit] = voxel_logit[need_assign_logit]
        # current_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:][need_assign_logit] = voxel_logit[need_assign_logit]
        # case 2
        updated[need_addition_logit] = prev_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:][need_addition_logit] + \
            voxel_logit[need_addition_logit] - self.prior_logit
        # current_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:][need_addition_logit] += \
        #     (voxel_logit[need_addition_logit] - self.prior_logit)
        
        is_post_occupaid = ~updated.isinf()
        updated[is_post_occupaid] = torch.clamp(updated[is_post_occupaid], min=-10, max=10)
        # is_post_occupaid = ~current_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:].isinf()
        # current_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:].clamp_(min=-10,max=10)

        
        # if the prob of a voxel is very low and is closely checked, then we set it to -10
        # NOTE: same reasoning as above.
        # However, as we don't know if the voxel is visible or not, we mark all voxels that
        # are both close and occupaid as low prob
        # we need to use an additional channel in order to know if the voxel has been 
        # closely looked at
        # confident_no_obj = ( updated < self.prior_logit ) & is_occupaid \
        #                 & (current_map[:, MC.BEEN_CLOSE_MAP].unsqueeze(1).repeat(1,self.max_mapped_height, 1,1)==1) # [B, H, W] -> [B, C, H, W
        confident_no_obj = confident_no_obj.unsqueeze(1).repeat(1,self.max_mapped_height, 1,1) # [B, H, W] -> [B, C, H, W]

        updated[confident_no_obj & is_post_occupaid] = -10
        current_map[:,MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:] = updated
        
        ############ how about use voxel map for prob map
        # is_post_occupaid_proj = is_post_occupaid.max(dim=1)[0]
        # current_map[:, MC.PROBABILITY_MAP, :, :][is_post_occupaid_proj] = \
        #     torch.max(current_map[:, MC.VOXEL_START:MC.NON_SEM_CHANNELS,:,:], dim=1)[0][is_post_occupaid_proj]
        # current_map[:, goal_idx :, :] = current_map[:,MC.PROBABILITY_MAP, :, :] > 0
        

        ############### end voxel update ###############
        # Reset current location
        # TODO: it is always in the center, do we need it?
        current_map[:, MC.CURRENT_LOCATION, :, :].fill_(0.0)
        curr_loc = current_pose[:, :2] 
        curr_loc = (curr_loc * 100.0 / self.xy_resolution).int()

        for e in range(batch_size):
            x, y = curr_loc[e]
            current_map[
                e,
                MC.CURRENT_LOCATION : MC.CURRENT_LOCATION + 2,
                y - 2 : y + 3,
                x - 2 : x + 3,
            ].fill_(1.0)

        return current_map, current_pose, instances_dict

    def _update_global_map_and_pose_for_env(
        self,
        e: int,
        local_map: Tensor,
        global_map: Tensor,
        local_pose: Tensor,
        global_pose: Tensor,
        lmb: Tensor,
        origins: Tensor,
    ):
        """Update global map and pose and re-center local map and pose for a
        particular environment.
        """
        global_map[e, :, lmb[e, 0] : lmb[e, 1], lmb[e, 2] : lmb[e, 3]] = local_map[e]
        global_pose[e] = local_pose[e] + origins[e]
        mu.recenter_local_map_and_pose_for_env(
            e,
            local_map,
            global_map,
            local_pose,
            global_pose,
            lmb,
            origins,
            self.map_size_parameters,
        )

    def _get_map_features(self, local_map: Tensor, global_map: Tensor) -> Tensor:
        """Get global and local map features.

        Arguments:
            local_map: local map of shape
             (batch_size, MC.NON_SEM_CHANNELS + num_sem_categories, M, M)
            global_map: global map of shape
             (batch_size, MC.NON_SEM_CHANNELS + num_sem_categories, M * ds, M * ds)

        Returns:
            map_features: semantic map features of shape
             (batch_size, 2 * MC.NON_SEM_CHANNELS + num_sem_categories, M, M)
        """
        map_features_channels = 2 * MC.NON_SEM_CHANNELS + self.num_sem_categories

        map_features = torch.zeros(
            local_map.size(0),
            map_features_channels,
            self.local_map_size,
            self.local_map_size,
            device=local_map.device,
            dtype=local_map.dtype,
        )

        # Local obstacles, explored area, and current and past position
        map_features[:, 0 : MC.NON_SEM_CHANNELS, :, :] = local_map[
            :, 0 : MC.NON_SEM_CHANNELS, :, :
        ]
        # Global obstacles, explored area, and current and past position
        map_features[
            :, MC.NON_SEM_CHANNELS : 2 * MC.NON_SEM_CHANNELS, :, :
        ] = nn.MaxPool2d(self.global_downscaling)(
            global_map[:, 0 : MC.NON_SEM_CHANNELS, :, :]
        )
        # Local semantic categories
        map_features[:, 2 * MC.NON_SEM_CHANNELS :, :, :] = local_map[
            :, MC.NON_SEM_CHANNELS :, :, :
        ]

        if debug_maps:
            plt.subplot(131)
            plt.imshow(local_map[0, 7])  # second object = cup
            plt.subplot(132)
            plt.imshow(local_map[0, 6])  # first object = chair
            # This is the channel in MAP FEATURES mode
            plt.subplot(133)
            plt.imshow(map_features[0, 12])
            plt.show()

        return map_features.detach()
