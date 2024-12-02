#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#
import time

import torch
import numpy as np
from utils.general_utils import inverse_sigmoid, get_expon_lr_func, build_rotation
from torch import nn
import os
from utils.system_utils import mkdir_p
from plyfile import PlyData, PlyElement
from utils.sh_utils import RGB2SH
from simple_knn._C import distCUDA2
from utils.graphics_utils import BasicPointCloud
from utils.general_utils import strip_symmetric, build_scaling_rotation,knn_in_plane,distance_between_points,detect_outliers_lof,manhattan_distance,point_to_plane_distance
from sklearn.neighbors import NearestNeighbors
from sklearn.cluster import DBSCAN
import open3d as o3d
from scipy.spatial.transform import Rotation

class GaussianModel:

    def setup_functions(self):
        def build_covariance_from_scaling_rotation(scaling, scaling_modifier, rotation):
            L = build_scaling_rotation(scaling_modifier * scaling, rotation)
            actual_covariance = L @ L.transpose(1, 2)
            symm = strip_symmetric(actual_covariance)
            return symm
        
        self.scaling_activation = torch.exp
        self.scaling_inverse_activation = torch.log

        self.covariance_activation = build_covariance_from_scaling_rotation

        self.opacity_activation = torch.sigmoid
        self.inverse_opacity_activation = inverse_sigmoid

        self.rotation_activation = torch.nn.functional.normalize


    def __init__(self, sh_degree : int):
        self.active_sh_degree = 0
        self.max_sh_degree = sh_degree

        self.knn_tree = None
        self._knn_index = {}
        self._normal = torch.empty(0)
        self._score = torch.empty(0)

        self._type = torch.empty(0)
        self._scene_scale = torch.empty(0)
        self._xyz = torch.empty(0)
        self._xyz_id = torch.empty(0)
        self.modify_id = []
        self._features_dc = torch.empty(0)
        self._features_rest = torch.empty(0)
        self._scaling = torch.empty(0)
        self._rotation = torch.empty(0)         # w, x, y, z
        self._opacity = torch.empty(0)
        self.max_radii2D = torch.empty(0)
        self.xyz_gradient_accum = torch.empty(0)
        self.position_gradient_accum = torch.empty(0)
        self.denom = torch.empty(0)
        self.optimizer = None
        self.percent_dense = 0
        self.spatial_lr_scale = 0
        self.distance_threshold = 0.03  ### 超参
        self.setup_functions()

    def capture(self):
        return (
            self.active_sh_degree,
            self._xyz,
            self._type, ### add
            self._normal, ### add
            self._features_dc,
            self._features_rest,
            self._scaling,
            self._rotation,
            self._opacity,
            self.max_radii2D,
            self.xyz_gradient_accum,
            self.denom,
            self.optimizer.state_dict(),
            self.spatial_lr_scale,
        )
    
    def restore(self, model_args, training_args):
        (self.active_sh_degree, 
        self._xyz, 
        self._features_dc, 
        self._features_rest,
        self._scaling, 
        self._rotation, 
        self._opacity,
        self.max_radii2D, 
        xyz_gradient_accum, 
        denom,
        opt_dict, 
        self.spatial_lr_scale) = model_args
        self.training_setup(training_args)
        self.xyz_gradient_accum = xyz_gradient_accum
        self.denom = denom
        self.optimizer.load_state_dict(opt_dict)

    @property
    def get_scene_scale(self):
        return self._scene_scale

    @property
    def get_scaling(self):
        return self.scaling_activation(self._scaling)
    
    @property
    def get_rotation(self):
        return self.rotation_activation(self._rotation)

    @property
    def get_type(self):
        return self._type

    @property
    def get_xyz(self):
        return self._xyz

    @property
    def get_xyz_id(self):
        return self._xyz_id

    def get_knn_index(self, k):
        return self._knn_index[k]

    def get_normal(self):
        return self._normal
    
    @property
    def get_features(self):
        features_dc = self._features_dc
        features_rest = self._features_rest
        return torch.cat((features_dc, features_rest), dim=1)
    
    @property
    def get_opacity(self):
        return self.opacity_activation(self._opacity)
    
    def get_covariance(self, scaling_modifier = 1):
        return self.covariance_activation(self.get_scaling, scaling_modifier, self._rotation)

    def oneupSHdegree(self):
        if self.active_sh_degree < self.max_sh_degree:
            self.active_sh_degree += 1

    def createKDTree(self, pc):
        # 创建KDTree
        self.knn_tree = o3d.geometry.KDTreeFlann(pc)

    def findKNN(self, k=4):
        # t1 = time.time()
        points_np = self.get_xyz.detach().cpu().numpy()
        pc = o3d.geometry.PointCloud()
        pc.points = o3d.utility.Vector3dVector(points_np)

        self.createKDTree(pc)

        # 寻找最近邻点
        self._knn_index[k] = [self.knn_tree.search_knn_vector_3d(p, knn=k)[1] for p in pc.points]

        # 转换为torch张量
        data = [torch.tensor(np.array(index)).unsqueeze(0) for index in self._knn_index[k]]
        data = torch.concat(data, dim=0).cuda()
        # t2 = time.time()
        # print('\nknn time(s) : ', f'{t2 - t1:.3f}')
        return data

    def computeNormal(self, k=30):

        return None
        # 创建点云
        t_1 = time.time()
        points_np = self.get_xyz.detach().cpu().numpy()
        pc = o3d.geometry.PointCloud()
        pc.points = o3d.utility.Vector3dVector(points_np)

        self.createKDTree(pc)

        # 寻找最近邻点
        list_k = [2, k]
        for _k in list_k:
            self._knn_index[_k] = [self.knn_tree.search_knn_vector_3d(p, knn=_k)[1] for p in pc.points]

        # 转换为torch张量
        data = [self.get_xyz[index, :].unsqueeze(0) for index in self._knn_index[k]]
        data = torch.concat(data, dim=0)

        # 计算法向量
        mean = torch.mean(data, dim=1, keepdim=True)
        # 减去均值
        centered_data = data - mean.expand_as(data)
        # 计算协方差矩阵
        covariance_matrix = torch.bmm(centered_data.permute(0, 2, 1), centered_data) / k
        # 使用svd函数进行奇异值分解
        U, S, _ = torch.linalg.svd(covariance_matrix)

        print(f'{time.time()-t_1} s')
        return U[:, :, 2]

    def reset_xyz_id(self):
        number_point = self._xyz.shape[0]
        index_id = np.arange(number_point)
        self._xyz_id = torch.tensor(index_id).cuda()

    def create_from_pcd(self, pcd : BasicPointCloud, spatial_lr_scale : float):
        self.spatial_lr_scale = spatial_lr_scale
        fused_point_cloud = torch.tensor(np.asarray(pcd.points)).float().cuda()
        fused_color = RGB2SH(torch.tensor(np.asarray(pcd.colors)).float().cuda())
        features = torch.zeros((fused_color.shape[0], 3, (self.max_sh_degree + 1) ** 2)).float().cuda()
        features[:, :3, 0 ] = torch.zeros_like(fused_color)
        features[:, 3:, 1:] = 0.0

        print("Number of points at initialisation : ", fused_point_cloud.shape[0])

        # dist2 = torch.clamp_min(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001)
        # scales = torch.log(torch.sqrt(dist2))[...,None].repeat(1, 3)
        # rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
        # rots[:, 0] = 1


        # 初始化主轴为xy轴，且长度为最近邻2倍
        dist2 = torch.clamp(distCUDA2(torch.from_numpy(np.asarray(pcd.points)).float().cuda()), 0.0000001, 0.01)

        scales = torch.sqrt(dist2 / 2)[..., None]
        zero = torch.full_like(scales, 1e-3)
        scales = torch.log(torch.concat([scales, scales, zero], dim=1))

        rots = torch.zeros((fused_point_cloud.shape[0], 4), device="cuda")
        directions = torch.tensor(np.asarray(pcd.normals)).float().cuda()
        directions_x = torch.ones_like(directions) - directions
        directions_x = torch.cross(directions, directions_x)
        directions_x /= torch.norm(directions_x, p=2, dim=1, keepdim=True)
        directions_y = torch.cross(directions, directions_x)

        rotations = torch.eye(3).float().cuda().unsqueeze(0).repeat(directions.shape[0], 1, 1)
        rotations[:, :, 0] = directions_x
        rotations[:, :, 1] = directions_y
        rotations[:, :, 2] = directions

        quats = Rotation.from_matrix(rotations.cpu().numpy()).as_quat()
        rots[:, 0] = torch.tensor(np.asarray(quats[:, 3])).float().cuda()
        rots[:, 1:4] = torch.tensor(np.asarray(quats[:, 0:3])).float().cuda()


        opacities = inverse_sigmoid(0.4 * torch.ones((fused_point_cloud.shape[0], 1), dtype=torch.float, device="cuda"))

        scene_scale = torch.tensor([1]).float().cuda()
        ### 确定点云的类型
        self._type = torch.tensor(np.asarray(pcd.types)).float().cuda()
        type_point_maks = (self._type == 0).squeeze()
        scales[:, 2][type_point_maks] = scales[:, 1][type_point_maks]

        self._scene_scale = nn.Parameter(scene_scale.requires_grad_(True))
        self._xyz = nn.Parameter(fused_point_cloud.requires_grad_(True))
        self._features_dc = nn.Parameter(features[:,:,0:1].transpose(1, 2).contiguous().requires_grad_(True))
        self._features_rest = nn.Parameter(features[:,:,1:].transpose(1, 2).contiguous().requires_grad_(True))
        self._scaling = nn.Parameter(scales.requires_grad_(True))
        self._rotation = nn.Parameter(rots.requires_grad_(True))
        self._opacity = nn.Parameter(opacities.requires_grad_(True))
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

        self.reset_xyz_id()

        self._normal = self.computeNormal()
        self._score = torch.sqrt(dist2)[..., None]

    def training_setup(self, training_args):
        self.percent_dense = training_args.percent_dense
        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.position_gradient_accum = torch.zeros((self.get_xyz.shape[0], 3), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")

        l = [
            {'params': [self._scene_scale], 'lr': training_args.scene_scale_lr_init, "name": "scene_scale"},
            {'params': [self._xyz], 'lr': training_args.position_lr_init * self.spatial_lr_scale, "name": "xyz"},
            {'params': [self._features_dc], 'lr': training_args.feature_lr, "name": "f_dc"},
            {'params': [self._features_rest], 'lr': training_args.feature_lr / 20.0, "name": "f_rest"},
            {'params': [self._opacity], 'lr': training_args.opacity_lr, "name": "opacity"},
            {'params': [self._scaling], 'lr': training_args.scaling_lr, "name": "scaling"},
            {'params': [self._rotation], 'lr': training_args.rotation_lr, "name": "rotation"}
        ]

        self.optimizer = torch.optim.Adam(l, lr=0.0, eps=1e-15)
        self.xyz_scheduler_args = get_expon_lr_func(lr_init=training_args.position_lr_init*self.spatial_lr_scale,
                                                    lr_final=training_args.position_lr_final*self.spatial_lr_scale,
                                                    lr_delay_mult=training_args.position_lr_delay_mult,
                                                    max_steps=training_args.position_lr_max_steps)

    def update_learning_rate(self, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in self.optimizer.param_groups:
            if param_group["name"] == "xyz":
                lr = self.xyz_scheduler_args(iteration)
                param_group['lr'] = lr
                return lr

    def construct_list_of_attributes(self):
        l = ['x', 'y', 'z', 'nx', 'ny', 'nz']
        # All channels except the 3 DC
        for i in range(self._features_dc.shape[1]*self._features_dc.shape[2]):
            l.append('f_dc_{}'.format(i))   # 3
        for i in range(self._features_rest.shape[1]*self._features_rest.shape[2]):
            l.append('f_rest_{}'.format(i)) # 45
        l.append('opacity')
        for i in range(self._scaling.shape[1]):
            l.append('scale_{}'.format(i)) # 3
        for i in range(self._rotation.shape[1]):
            l.append('rot_{}'.format(i))  # 4
        # for i in range(self._normal.shape[1]):
        #     l.append('normal_{}'.format(i))  # 3
        l.append('type')   # 1
        # for i in range(self._score.shape[1]):
        #     l.append('score_{}'.format(i))
        return l

    def save_ply(self, path):
        mkdir_p(os.path.dirname(path))

        xyz = self._xyz.detach().cpu().numpy()
        # normals = np.zeros_like(xyz)
        normals = torch.from_numpy(self._normal)

        normals = normals.detach().cpu().numpy()
        types = self._type
        types = types.detach().cpu().numpy()
        f_dc = self._features_dc.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        f_rest = self._features_rest.detach().transpose(1, 2).flatten(start_dim=1).contiguous().cpu().numpy()
        opacities = self._opacity.detach().cpu().numpy()
        scale = self._scaling.detach().cpu().numpy()
        rotation = self._rotation.detach().cpu().numpy()

        # score = self._score.detach().cpu().numpy()

        dtype_full = [(attribute, 'f4') for attribute in self.construct_list_of_attributes()]

        elements = np.empty(xyz.shape[0], dtype=dtype_full)
        # attributes = np.concatenate((xyz, normals, f_dc, f_rest, opacities, scale, rotation, score), axis=1)
        attributes = np.concatenate((xyz, normals,  f_dc, f_rest, opacities, scale, rotation ,types), axis=1)
        elements[:] = list(map(tuple, attributes))
        el = PlyElement.describe(elements, 'vertex')
        PlyData([el]).write(path)

    def reset_opacity(self):
        opacities_new = inverse_sigmoid(torch.min(self.get_opacity, torch.ones_like(self.get_opacity)*0.01))
        optimizable_tensors = self.replace_tensor_to_optimizer(opacities_new, "opacity")
        self._opacity = optimizable_tensors["opacity"]

    def load_ply(self, path):
        plydata = PlyData.read(path)

        xyz = np.stack((np.asarray(plydata.elements[0]["x"]),
                        np.asarray(plydata.elements[0]["y"]),
                        np.asarray(plydata.elements[0]["z"])),  axis=1)
        opacities = np.asarray(plydata.elements[0]["opacity"])[..., np.newaxis]

        features_dc = np.zeros((xyz.shape[0], 3, 1))
        features_dc[:, 0, 0] = np.asarray(plydata.elements[0]["f_dc_0"])
        features_dc[:, 1, 0] = np.asarray(plydata.elements[0]["f_dc_1"])
        features_dc[:, 2, 0] = np.asarray(plydata.elements[0]["f_dc_2"])

        extra_f_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("f_rest_")]
        extra_f_names = sorted(extra_f_names, key = lambda x: int(x.split('_')[-1]))
        assert len(extra_f_names)==3*(self.max_sh_degree + 1) ** 2 - 3
        features_extra = np.zeros((xyz.shape[0], len(extra_f_names)))
        for idx, attr_name in enumerate(extra_f_names):
            features_extra[:, idx] = np.asarray(plydata.elements[0][attr_name])
        # Reshape (P,F*SH_coeffs) to (P, F, SH_coeffs except DC)
        features_extra = features_extra.reshape((features_extra.shape[0], 3, (self.max_sh_degree + 1) ** 2 - 1))

        scale_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("scale_")]
        scale_names = sorted(scale_names, key = lambda x: int(x.split('_')[-1]))
        scales = np.zeros((xyz.shape[0], len(scale_names)))
        for idx, attr_name in enumerate(scale_names):
            scales[:, idx] = np.asarray(plydata.elements[0][attr_name])

        rot_names = [p.name for p in plydata.elements[0].properties if p.name.startswith("rot")]
        rot_names = sorted(rot_names, key = lambda x: int(x.split('_')[-1]))
        rots = np.zeros((xyz.shape[0], len(rot_names)))
        for idx, attr_name in enumerate(rot_names):
            rots[:, idx] = np.asarray(plydata.elements[0][attr_name])

        self._xyz = nn.Parameter(torch.tensor(xyz, dtype=torch.float, device="cuda").requires_grad_(True))
        self._features_dc = nn.Parameter(torch.tensor(features_dc, dtype=torch.float, device="cuda").transpose(1, 2).contiguous().requires_grad_(True))
        self._features_rest = nn.Parameter(torch.tensor(features_extra, dtype=torch.float, device="cuda").transpose(1, 2).contiguous().requires_grad_(True))
        self._opacity = nn.Parameter(torch.tensor(opacities, dtype=torch.float, device="cuda").requires_grad_(True))
        self._scaling = nn.Parameter(torch.tensor(scales, dtype=torch.float, device="cuda").requires_grad_(True))
        self._rotation = nn.Parameter(torch.tensor(rots, dtype=torch.float, device="cuda").requires_grad_(True))

        self.active_sh_degree = self.max_sh_degree

    def replace_tensor_to_optimizer(self, tensor, name):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == name:
                stored_state = self.optimizer.state.get(group['params'][0], None)
                stored_state["exp_avg"] = torch.zeros_like(tensor)
                stored_state["exp_avg_sq"] = torch.zeros_like(tensor)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(tensor.requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def _prune_optimizer(self, mask):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            if group["name"] == 'scene_scale':
                continue
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:
                stored_state["exp_avg"] = stored_state["exp_avg"][mask]
                stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][mask]

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter((group["params"][0][mask].requires_grad_(True)))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(group["params"][0][mask].requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]
        return optimizable_tensors

    def prune_points(self, mask):
        valid_points_mask = ~mask  ### 选中有效点 也就是不剪掉的点
        optimizable_tensors = self._prune_optimizer(valid_points_mask)

        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]


        self.xyz_gradient_accum = self.xyz_gradient_accum[valid_points_mask]
        self.position_gradient_accum = self.position_gradient_accum[valid_points_mask]


        self.denom = self.denom[valid_points_mask]
        self.max_radii2D = self.max_radii2D[valid_points_mask]
        self._score = self._score[valid_points_mask]
        self._xyz_id = self._xyz_id[valid_points_mask]
        self._type = self._type[valid_points_mask]

    def cat_tensors_to_optimizer(self, tensors_dict):
        optimizable_tensors = {}
        for group in self.optimizer.param_groups:
            assert len(group["params"]) == 1
            if group["name"] == 'scene_scale':
                continue
            extension_tensor = tensors_dict[group["name"]]
            stored_state = self.optimizer.state.get(group['params'][0], None)
            if stored_state is not None:

                stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(extension_tensor)), dim=0)
                stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(extension_tensor)), dim=0)

                del self.optimizer.state[group['params'][0]]
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                self.optimizer.state[group['params'][0]] = stored_state

                optimizable_tensors[group["name"]] = group["params"][0]
            else:
                group["params"][0] = nn.Parameter(torch.cat((group["params"][0], extension_tensor), dim=0).requires_grad_(True))
                optimizable_tensors[group["name"]] = group["params"][0]

        return optimizable_tensors

    def densification_postfix(self, new_xyz, new_features_dc, new_features_rest, new_opacities, new_scaling, new_rotation, new_score, new_xyz_id, new_type):
        d = {"xyz": new_xyz,
        "f_dc": new_features_dc,
        "f_rest": new_features_rest,
        "opacity": new_opacities,
        "scaling" : new_scaling,
        "rotation" : new_rotation}

        optimizable_tensors = self.cat_tensors_to_optimizer(d)
        self._xyz = optimizable_tensors["xyz"]
        self._features_dc = optimizable_tensors["f_dc"]
        self._features_rest = optimizable_tensors["f_rest"]
        self._opacity = optimizable_tensors["opacity"]
        self._scaling = optimizable_tensors["scaling"]
        self._rotation = optimizable_tensors["rotation"]

        self.xyz_gradient_accum = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.position_gradient_accum = torch.zeros((self.get_xyz.shape[0], 3), device="cuda")
        self.denom = torch.zeros((self.get_xyz.shape[0], 1), device="cuda")
        self.max_radii2D = torch.zeros((self.get_xyz.shape[0]), device="cuda")

        self._score = torch.concat([self._score, new_score], dim=0)
        self._xyz_id = torch.concat([self._xyz_id, new_xyz_id], dim=0)
        self._type = torch.concat([self._type, new_type], dim=0)

    def densify_and_split(self, grads, grad_threshold, scene_extent, N=2):
        n_init_points = self.get_xyz.shape[0]
        # Extract points that satisfy the gradient condition
        padded_grad = torch.zeros((n_init_points), device="cuda")
        padded_grad[:grads.shape[0]] = grads.squeeze()
        selected_pts_mask = torch.where(padded_grad >= grad_threshold, True, False) ### 切割的条件是缩放尺寸大于阈值且地图优化不全
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values > self.percent_dense*scene_extent)

        stds = self.get_scaling[selected_pts_mask].repeat(N,1)
        means =torch.zeros((stds.size(0), 3),device="cuda")
        samples = torch.normal(mean=means, std=stds)
        rots = build_rotation(self._rotation[selected_pts_mask]).repeat(N,1,1)
        new_xyz = torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1) + self.get_xyz[selected_pts_mask].repeat(N, 1)
        new_scaling = self.scaling_inverse_activation(self.get_scaling[selected_pts_mask].repeat(N,1) / (0.8*N))
        new_rotation = self._rotation[selected_pts_mask].repeat(N,1)
        new_features_dc = self._features_dc[selected_pts_mask].repeat(N,1,1)
        new_features_rest = self._features_rest[selected_pts_mask].repeat(N,1,1)
        new_opacity = self._opacity[selected_pts_mask].repeat(N,1)
        new_score = self._score[selected_pts_mask].repeat(N, 1)
        new_xyz_id = self._xyz_id[selected_pts_mask].repeat(N)
        new_type = self._type[selected_pts_mask].repeat(N, 1)

        self.modify_id.extend(self._xyz_id[selected_pts_mask].tolist())

        self.densification_postfix(new_xyz, new_features_dc, new_features_rest, new_opacity, new_scaling, new_rotation, new_score, new_xyz_id, new_type)

        prune_filter = torch.cat((selected_pts_mask, torch.zeros(N * selected_pts_mask.sum(), device="cuda", dtype=bool)))
        self.prune_points(prune_filter)

    def densify_and_clone(self, grads, grad_threshold, scene_extent):
        # Extract points that satisfy the gradient condition
        selected_pts_mask = torch.where(torch.norm(grads, dim=-1) >= grad_threshold, True, False)  ### 克隆的执行前提是缩放尺寸小于阈值 梯度优化大于阈值
        selected_pts_mask = torch.logical_and(selected_pts_mask,
                                              torch.max(self.get_scaling, dim=1).values <= self.percent_dense*scene_extent)

        xyz_lr = 1
        # for param_group in self.optimizer.param_groups:
        #     if param_group["name"] == "xyz":
        #         xyz_lr = param_group['lr']
        #         break

        # new_xyz = self._xyz[selected_pts_mask] + xyz_lr * self.position_gradient_accum[selected_pts_mask] / self.denom[selected_pts_mask]
        # new_xyz = self._xyz[selected_pts_mask] + xyz_lr * self.position_gradient_accum[selected_pts_mask] / self.denom[selected_pts_mask]
        new_xyz = self._xyz[selected_pts_mask]
        new_features_dc = self._features_dc[selected_pts_mask]
        new_features_rest = self._features_rest[selected_pts_mask]
        new_opacities = self._opacity[selected_pts_mask]
        new_scaling = self._scaling[selected_pts_mask]
        new_rotation = self._rotation[selected_pts_mask]
        new_score = self._score[selected_pts_mask]
        new_xyz_id = self._xyz_id[selected_pts_mask]
        new_type = self._type[selected_pts_mask]
        selected_pts_mask = torch.logical_and(selected_pts_mask, self._type.squeeze() == 1)
        new_xyz[new_type.squeeze() == 1] += xyz_lr * self.position_gradient_accum[selected_pts_mask] / self.denom[selected_pts_mask]
        self.modify_id.extend(new_xyz_id.tolist())

        self.densification_postfix(new_xyz, new_features_dc, new_features_rest, new_opacities, new_scaling, new_rotation, new_score, new_xyz_id, new_type)

    def densify_and_prune(self, max_grad, min_opacity, extent, max_screen_size):
        ### 是否要执行 处理前的减点
        # outliers_points = torch.tensor(detect_outliers_lof(self.get_xyz), device="cuda")
        # prune_mask = ~outliers_points
        # self.prune_points(prune_mask)

        grads = self.xyz_gradient_accum / self.denom
        grads[grads.isnan()] = 0.0

        self.reset_xyz_id()
        self.modify_id = []


        # if self.get_xyz.shape[0] <1600_000:
        self.densify_and_clone(grads, max_grad, extent)
        self.densify_and_split(grads, max_grad, extent)

        prune_mask = (self.get_opacity < min_opacity).squeeze() ## True代表要消除的 False代表不消除的
        # outliers_points =  torch.tensor(detect_outliers_lof(self.get_xyz), device="cuda")
        points_np = self.get_xyz
        # 使用DBSCAN进行聚类
        # eps 是点的最大距离，min_samples 是形成簇的最小点数
        # dbscan = DBSCAN(eps=0.05, min_samples=10)
        # labels = dbscan.fit_predict(points_np)
        # # 获取每个簇的大小
        # unique_labels, counts = np.unique(labels, return_counts=True)
        #
        # # 找到最大簇的标签（排除噪声点）
        # largest_cluster_label = unique_labels[counts.argmax()]
        #
        # # 创建掩码以筛选最大簇
        # largest_cluster_mask = labels == largest_cluster_label

        # 筛选出最大簇的点
        # filtered_points = points_np[largest_cluster_mask]
        # prune_mask = torch.logical_or(prune_mask,~outliers_points)
        # prune_mask = torch.logical_or(prune_mask, ~filtered_points)
        if max_screen_size:
            big_points_vs = self.max_radii2D > max_screen_size
            big_points_ws = self.get_scaling.max(dim=1).values > 0.1 * extent
            prune_mask = torch.logical_or(torch.logical_or(prune_mask, big_points_vs), big_points_ws)
        self.prune_points(prune_mask)

        # 稠密化后，重新计算法向量
        # self._normal = self.computeNormal()
        #
        # # A = self._normal  # A是none
        # ### 在此处编写新的type计算方式？
        # ### pcd怎么读取？？？？
        points_np = self.get_xyz.detach().cpu().numpy()
        pc = o3d.geometry.PointCloud()
        pc.points = o3d.utility.Vector3dVector(points_np)
        pc.estimate_normals(
            search_param=o3d.geometry.KDTreeSearchParamKNN(knn=9))  ### 估计点云法向量 最近邻
        # o3d.visualization.draw_geometries([pcd], point_show_normal=True)

        normals = np.array(pc.normals)
        # 稠密化后，重新计算法向量
        self._normal = normals
        # knn_tree = o3d.geometry.KDTreeFlann(pc)

        # # 寻找最近邻点  # 如果当前点及其最近邻点的法向量夹角均小于 0.03 弧度，则将这些点的类型标记为 1
        # ### 是否应该每次计算前都要重置为0
        # self._type = torch.zeros((self._normal.shape[0], 1)).cuda()
        # k = 4 # 5
        # search_radius = 0.1 ## 新的超参
        # knn_indexs = [knn_tree.search_knn_vector_3d(p,knn=k)[1] for p in pc.points]
        # # knn_indexs = []
        # # for i, point in enumerate(pc.points):
        # #     [num_neighbors, single_knn_indexs, _] = knn_tree.search_hybrid_vector_3d(point, search_radius, k)
        # #     if num_neighbors < k:
        # #         continue  # 如果找到的邻居点数量不足 k，则跳过
        # #     else:
        # #         knn_indexs.append(single_knn_indexs)
        # # i = 0
        # # knn_indexs = []
        # # for p in pc.points:
        # #     single_knn_indexs = knn_in_plane(pc.points,normals,p,normals[i])
        # #     knn_indexs.append(single_knn_indexs)
        #
        # for knn_index in knn_indexs:
        #     distance = 0
        #     is_valid = True
        #     is_distance = True
        #     current_normal = normals[knn_index[0]]
        #     current_point = pc.points[knn_index[0]]
        #     for i in range(k):
        #         distance = distance + distance_between_points(current_point,pc.points[knn_index[i]])
        #         # distance = distance + point_to_plane_distance(current_point, current_normal ,pc.points[knn_index[i]])
        #         # distance = distance + manhattan_distance(current_point,pc.points[knn_index[i]])
        #     for idx in range(k):
        #         if np.sum(current_normal * normals[knn_index[idx]]) < 1 - np.cos(0.03): ### < 1 - np.cos(0.03)是指的夹角大于0.03
        #             is_valid = False
        #             break
        #     if is_valid : ### 表明是位于平滑区域的椭圆球
        #         if distance < self.distance_threshold:  ### 表示在一个平面内的距离感知  或许应改为投影感知？？ 就是在同一平面内？
        #             for idx in range(k):
        #                 self._type[knn_index[idx]] = 1
        # if self.distance_threshold > 0.001:
        #     self.distance_threshold  = self.distance_threshold - 0.0002
        # else:
        #     self.distance_threshold = 0.001




        torch.cuda.empty_cache()

    def add_densification_stats(self, viewspace_point_tensor, update_filter):
        self.xyz_gradient_accum[update_filter] += torch.norm(viewspace_point_tensor.grad[update_filter,:2], dim=-1, keepdim=True)
        self.position_gradient_accum[update_filter] += self._xyz.grad[update_filter]
        self.denom[update_filter] += 1