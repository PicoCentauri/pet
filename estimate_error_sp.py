from utilities import get_all_species, get_compositional_features
import os

import torch
import ase.io
import numpy as np
from multiprocessing import cpu_count
from pathos.multiprocessing import ProcessingPool as Pool
from tqdm import tqdm
import torch_geometric
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader, DataListLoader
from torch import nn
import torch.nn.functional as F
from sklearn.linear_model import Ridge
from utilities import ModelKeeper
import time
from scipy.spatial.transform import Rotation
from torch.optim.lr_scheduler import LambdaLR
import sys
import copy
import inspect
import yaml
from torch_geometric.nn import DataParallel

from molecule import Molecule, batch_to_dict
from hypers import Hypers
from pet import PET
from utilities import FullLogger
from utilities import get_rmse, get_mae, get_relative_rmse, get_loss
from analysis import get_structural_batch_size, convert_atomic_throughput


from sp_frames_calculator import SPFramesCalculator, SPHypers

STRUCTURES_PATH = sys.argv[1]
HYPERS_PATH = sys.argv[2]
PATH_TO_MODEL_STATE_DICT = sys.argv[3]
ALL_SPECIES_PATH = sys.argv[4]
SELF_CONTRIBUTIONS_PATH = sys.argv[5]

bool_map = {'True' : True, 'False' : False}
USE_AUG = bool_map[sys.argv[6]]
N_AUG = int(sys.argv[7])

hypers = Hypers()
hypers.load_from_file(HYPERS_PATH)
structures = ase.io.read(STRUCTURES_PATH, index = ':100')

all_species = np.load(ALL_SPECIES_PATH)
if hypers.USE_ENERGIES:
    self_contributions = np.load(SELF_CONTRIBUTIONS_PATH)

molecules = [Molecule(structure, hypers.R_CUT, hypers.USE_ADDITIONAL_SCALAR_ATTRIBUTES, hypers.USE_FORCES) for structure in tqdm(structures)]
max_nums = [molecule.get_max_num() for molecule in molecules]
max_num = np.max(max_nums)
graphs = [molecule.get_graph(max_num, all_species) for molecule in tqdm(molecules)]

if hypers.MULTI_GPU:
    loader = DataListLoader(graphs, 1, shuffle=False)
else:        
    loader = DataLoader(graphs, 1, shuffle=False)

add_tokens = []
for _ in range(hypers.N_GNN_LAYERS - 1):
    add_tokens.append(hypers.ADD_TOKEN_FIRST)
add_tokens.append(hypers.ADD_TOKEN_SECOND)

model = PET(hypers, hypers.TRANSFORMER_D_MODEL, hypers.TRANSFORMER_N_HEAD,
                       hypers.TRANSFORMER_DIM_FEEDFORWARD, hypers.N_TRANS_LAYERS, 
                       0.0, len(all_species), 
                       hypers.N_GNN_LAYERS, hypers.HEAD_N_NEURONS, hypers.TRANSFORMERS_CENTRAL_SPECIFIC, hypers.HEADS_CENTRAL_SPECIFIC, 
                       add_tokens).cuda()
if hypers.MULTI_GPU:
    model = DataParallel(model)
    device = torch.device('cuda:0')
    model = model.to(device)
    
model.load_state_dict(torch.load(PATH_TO_MODEL_STATE_DICT))
model.eval()

if hypers.USE_ENERGIES:
    energies_ground_truth = np.array([struc.info['energy'] for struc in structures])
    
if hypers.USE_FORCES:
    forces_ground_truth = [struc.arrays['forces'] for struc in structures]
    forces_ground_truth = np.concatenate(forces_ground_truth, axis = 0)
    
    
class PETSP(torch.nn.Module):
    def __init__(self, pet, sp_frames_calculator, additional_rotations = None):
        super(PETSP, self).__init__()
        self.pet = pet
        self.pet.task = 'energies'
        self.sp_frames_calculator = sp_frames_calculator
        self.task = 'both'
        self.additional_rotations = additional_rotations
        if self.additional_rotations is None:
            self.additional_rotations = [torch.eye(3)]
        
    def get_all_frames(self, batch):
        all_envs = []
        for env_index in range(batch.x.shape[0]):
            mask_now = torch.logical_not(batch.mask[env_index])
            env_now = batch.x[env_index][mask_now]
            all_envs.append(env_now)
            
        r_cut = torch.tensor(hypers.R_CUT, device = batch.x.device)
        return self.sp_frames_calculator.get_all_frames_global(all_envs, r_cut)
    
    def forward(self, batch):        
        if self.task == 'both':
            return self.get_targets(batch)        
        
        frames, weights = self.get_all_frames(batch)
        batch.x_initial = batch.x
        
        predictions_accumulated = 0.0
        weight_accumulated = 0.0
        
        for additional_rotation in self.additional_rotations:
            additional_rotation = additional_rotation.to(batch.x.device)
            for frame, weight in zip(frames, weights):
                frame = torch.matmul(additional_rotation, frame)
                frame = frame[None]
                frame = frame.repeat(batch.x_initial.shape[0], 1, 1)
                batch.x = torch.bmm(batch.x_initial, frame)
                predictions_now = self.pet(batch)
                #print(predictions_now)
                predictions_accumulated = predictions_accumulated + predictions_now * weight
                weight_accumulated += weight
        return predictions_accumulated / weight_accumulated
    
    def get_targets(self, batch):
        
        batch.x_initial = batch.x.clone().detach()
        batch.x_initial.requires_grad = True
        batch.x = batch.x_initial
        
        self.task = 'energies'
        predictions = self(batch)
        self.task = 'both'
        if hypers.USE_FORCES:
            grads  = torch.autograd.grad(predictions, batch.x_initial, grad_outputs = torch.ones_like(predictions),
                                    create_graph = True)[0]
            neighbors_index = batch.neighbors_index.transpose(0, 1)
            neighbors_pos = batch.neighbors_pos
            grads_messaged = grads[neighbors_index, neighbors_pos]
            grads[batch.mask] = 0.0
            first = grads.sum(dim = 1)
            grads_messaged[batch.mask] = 0.0
            second = grads_messaged.sum(dim = 1)
        
        result = []
        if hypers.USE_ENERGIES:
            result.append(predictions)
            result.append(batch.y)
        else:
            result.append(None)
            result.append(None)
            
        if hypers.USE_FORCES:
            result.append(first - second)
            result.append(batch.forces)
        else:
            result.append(None)
            result.append(None)
            
        return result
    

sp_hypers = SPHypers(2.0, 0.5, 0.2)
sp_frames_calculator = SPFramesCalculator(sp_hypers)

if USE_AUG:
    additional_rotations = [torch.FloatTensor(el) for el in Rotation.random(N_AUG).as_matrix()]
else:
    additional_rotations = None
    
model_sp = PETSP(model, sp_frames_calculator, additional_rotations = additional_rotations).cuda()
if hypers.USE_ENERGIES:
    all_energies_predicted = []
    
if hypers.USE_FORCES:
    all_forces_predicted = []
    
if hypers.USE_ENERGIES:
    energies_predicted = []
if hypers.USE_FORCES:
    forces_predicted = []

for batch in tqdm(loader):
    batch.cuda()
    predictions_energies, targets_energies, predictions_forces, targets_forces = model_sp(batch)
    if hypers.USE_ENERGIES:
        energies_predicted.append(predictions_energies.data.cpu().numpy())
    if hypers.USE_FORCES:
        forces_predicted.append(predictions_forces.data.cpu().numpy())

if hypers.USE_ENERGIES:
    energies_predicted = np.concatenate(energies_predicted, axis = 0)
    all_energies_predicted.append(energies_predicted)

if hypers.USE_FORCES:
    forces_predicted = np.concatenate(forces_predicted, axis = 0)
    all_forces_predicted.append(forces_predicted)
        
        
if hypers.USE_ENERGIES:
    all_energies_predicted = [el[np.newaxis] for el in all_energies_predicted]
    all_energies_predicted = np.concatenate(all_energies_predicted, axis = 0)
    energies_predicted_mean = np.mean(all_energies_predicted, axis = 0)
    
if hypers.USE_FORCES:
    all_forces_predicted = [el[np.newaxis] for el in all_forces_predicted]
    all_forces_predicted = np.concatenate(all_forces_predicted, axis = 0)
    forces_predicted_mean = np.mean(all_forces_predicted, axis = 0)

if hypers.USE_ENERGIES:
    
    compositional_features = get_compositional_features(structures, all_species)
    self_contributions_energies = []
    for i in range(len(structures)):
        self_contributions_energies.append(np.dot(compositional_features[i], self_contributions))
    self_contributions_energies = np.array(self_contributions_energies)
    
    energies_predicted_mean = energies_predicted_mean + self_contributions_energies
    
    print(f"energies mae: {get_mae(energies_ground_truth, energies_predicted_mean)}")
    print(f"energies rmse: {get_rmse(energies_ground_truth, energies_predicted_mean)}")
    
if hypers.USE_FORCES:
    print(f"forces mae per component: {get_mae(forces_ground_truth, forces_predicted_mean)}")
    print(f"forces rmse per component: {get_rmse(forces_ground_truth, forces_predicted_mean)}")