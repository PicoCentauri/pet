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
import random
from molecule import Molecule, batch_to_dict
from hypers import Hypers
from pet import PET
from utilities import FullLogger
from utilities import get_rmse, get_mae, get_relative_rmse, get_loss
from analysis import get_structural_batch_size, convert_atomic_throughput
import argparse

parser = argparse.ArgumentParser()


parser.add_argument("structures_path", help="Path to an xyz file with structures", type = str)
parser.add_argument("path_to_calc_folder", help="Path to a folder with a model to use", type = str)
parser.add_argument("checkpoint", help="Path to a particular checkpoint to use", type = str, choices = ['best_val_mae_direct_targets_model', 'best_val_rmse_direct_targets_model', 'best_val_mae_target_grads_model', 'best_val_rmse_target_grads_model',  'best_val_mae_both_model', 'best_val_rmse_both_model'])

parser.add_argument("n_aug", type = int, help = "A number of rotational augmentations to use. It should be a positive integer")
parser.add_argument("default_hypers_path", help="Path to a YAML file with default hypers", type = str)

parser.add_argument("batch_size", type = int, help="Batch size to use for inference. It should be a positive integer or -1. If -1, it will be set to the value used for fitting the provided model.")

parser.add_argument("--path_save_predictions", help="Path to a folder where to save predictions.", type = str)
parser.add_argument("--verbose", help="Show more details",
                    action="store_true")

args = parser.parse_args()

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

HYPERS_PATH = args.path_to_calc_folder + '/hypers_used.yaml'
PATH_TO_MODEL_STATE_DICT = args.path_to_calc_folder + '/' + args.checkpoint + '_state_dict'
ALL_SPECIES_PATH = args.path_to_calc_folder + '/all_species.npy'
SELF_CONTRIBUTIONS_PATH = args.path_to_calc_folder + '/self_contributions.npy'



hypers = Hypers()
# loading default values for the new hypers potentially added into the codebase after the calculation is done
# assuming that the default values do not change the logic
hypers.set_from_files(HYPERS_PATH, args.default_hypers_path, check_dublicated = False)

torch.manual_seed(hypers.RANDOM_SEED)
np.random.seed(hypers.RANDOM_SEED)
random.seed(hypers.RANDOM_SEED)
os.environ['PYTHONHASHSEED'] = str(hypers.RANDOM_SEED)
if torch.cuda.is_available():
    torch.cuda.manual_seed(hypers.RANDOM_SEED)
    torch.cuda.manual_seed_all(hypers.RANDOM_SEED)

if args.batch_size == -1:
    args.batch_size = hypers.STRUCTURAL_BATCH_SIZE

    
structures = ase.io.read(args.structures_path, index = ':')

all_species = np.load(ALL_SPECIES_PATH)
if hypers.USE_DIRECT_TARGETS:
    self_contributions = np.load(SELF_CONTRIBUTIONS_PATH)

molecules = [Molecule(structure, hypers.R_CUT, hypers.USE_ADDITIONAL_SCALAR_ATTRIBUTES, hypers.USE_TARGET_GRADS) for structure in tqdm(structures)]
max_nums = [molecule.get_max_num() for molecule in molecules]
max_num = np.max(max_nums)
graphs = [molecule.get_graph(max_num, all_species) for molecule in tqdm(molecules)]

if hypers.MULTI_GPU:
    loader = DataListLoader(graphs, batch_size=args.batch_size, shuffle=False)
else:        
    loader = DataLoader(graphs, batch_size=args.batch_size, shuffle=False)

add_tokens = []
for _ in range(hypers.N_GNN_LAYERS - 1):
    add_tokens.append(hypers.ADD_TOKEN_FIRST)
add_tokens.append(hypers.ADD_TOKEN_SECOND)

model = PET(hypers, hypers.TRANSFORMER_D_MODEL, hypers.TRANSFORMER_N_HEAD,
                       hypers.TRANSFORMER_DIM_FEEDFORWARD, hypers.N_TRANS_LAYERS, 
                       0.0, len(all_species), 
                       hypers.N_GNN_LAYERS, hypers.HEAD_N_NEURONS, hypers.TRANSFORMERS_CENTRAL_SPECIFIC, hypers.HEADS_CENTRAL_SPECIFIC, 
                       add_tokens).to(device)

if hypers.MULTI_GPU and torch.cuda.is_available():
    model = DataParallel(model)
    model = model.to( torch.device('cuda:0'))
    
model.load_state_dict(torch.load(PATH_TO_MODEL_STATE_DICT))
model.eval()

if hypers.USE_DIRECT_TARGETS:
    direct_targets_ground_truth = np.array([struc.info['direct_targets'] for struc in structures])
    
if hypers.USE_TARGET_GRADS:
    target_grads_ground_truth = [struc.arrays['target_grads'] for struc in structures]
    target_grads_ground_truth = np.concatenate(target_grads_ground_truth, axis = 0)
    
    

if hypers.USE_DIRECT_TARGETS:
    all_direct_targets_predicted = []
    
if hypers.USE_TARGET_GRADS:
    all_target_grads_predicted = []
    
#warmup for correct time estimation
for batch in loader:
    if not hypers.MULTI_GPU:
        batch.to(device)
        model.augmentation = True
    else:
        model.module.augmentation = True

    predictions_direct_targets, targets_direct_targets, predictions_target_grads, targets_target_grads = model(batch)
    break
    
begin = time.time()
for _ in tqdm(range(args.n_aug)):
    if hypers.USE_DIRECT_TARGETS:
        direct_targets_predicted = []
    if hypers.USE_TARGET_GRADS:
        target_grads_predicted = []
    
    for batch in loader:
        if not hypers.MULTI_GPU:
            batch.to(device)
            model.augmentation = True
        else:
            model.module.augmentation = True
            
        predictions_direct_targets, targets_direct_targets, predictions_target_grads, targets_target_grads = model(batch)
        if hypers.USE_DIRECT_TARGETS:
            direct_targets_predicted.append(predictions_direct_targets.data.cpu().numpy())
        if hypers.USE_TARGET_GRADS:
            target_grads_predicted.append(predictions_target_grads.data.cpu().numpy())
            
    if hypers.USE_DIRECT_TARGETS:
        direct_targets_predicted = np.concatenate(direct_targets_predicted, axis = 0)
        all_direct_targets_predicted.append(direct_targets_predicted)
        
    if hypers.USE_TARGET_GRADS:
        target_grads_predicted = np.concatenate(target_grads_predicted, axis = 0)
        all_target_grads_predicted.append(target_grads_predicted)
        
total_time = time.time() - begin
n_atoms = np.array([len(struc.positions) for struc in structures])
time_per_atom = total_time / (np.sum(n_atoms) * args.n_aug)
 
if hypers.USE_DIRECT_TARGETS:
    all_direct_targets_predicted = [el[np.newaxis] for el in all_direct_targets_predicted]
    all_direct_targets_predicted = np.concatenate(all_direct_targets_predicted, axis = 0)
    direct_targets_predicted_mean = np.mean(all_direct_targets_predicted, axis = 0)
    
    
    if all_direct_targets_predicted.shape[0] > 1:
        direct_targets_rotational_discrepancies = all_direct_targets_predicted - direct_targets_predicted_mean[np.newaxis]
        print('direct_targets_rotational_discrepancies', direct_targets_rotational_discrepancies.shape)
        direct_targets_rotational_discrepancies_per_atom = direct_targets_rotational_discrepancies / n_atoms[np.newaxis, :]
        correction = all_direct_targets_predicted.shape[0] / (all_direct_targets_predicted.shape[0] - 1)
        direct_targets_rotational_std_per_atom = np.sqrt(np.mean(direct_targets_rotational_discrepancies_per_atom ** 2) * correction)
        
    
    compositional_features = get_compositional_features(structures, all_species)
    self_contributions_direct_targets = []
    for i in range(len(structures)):
        self_contributions_direct_targets.append(np.dot(compositional_features[i], self_contributions))
    self_contributions_direct_targets = np.array(self_contributions_direct_targets)
    
    direct_targets_predicted_mean = direct_targets_predicted_mean + self_contributions_direct_targets
    
    print(f"direct_targets mae per struc: {get_mae(direct_targets_ground_truth, direct_targets_predicted_mean)}")
    print(f"direct_targets rmse per struc: {get_rmse(direct_targets_ground_truth, direct_targets_predicted_mean)}")
    
    
    direct_targets_predicted_mean_per_atom = direct_targets_predicted_mean / n_atoms
    direct_targets_ground_truth_per_atom = direct_targets_ground_truth / n_atoms
    
    print(f"direct_targets mae per atom: {get_mae(direct_targets_ground_truth_per_atom, direct_targets_predicted_mean_per_atom)}")
    print(f"direct_targets rmse per atom: {get_rmse(direct_targets_ground_truth_per_atom, direct_targets_predicted_mean_per_atom)}")
    
    if all_direct_targets_predicted.shape[0] > 1:
        if args.verbose:
            print(f"direct_targets rotational discrepancy std per atom: {direct_targets_rotational_std_per_atom}")
    
    
if hypers.USE_TARGET_GRADS:
    all_target_grads_predicted = [el[np.newaxis] for el in all_target_grads_predicted]
    all_target_grads_predicted = np.concatenate(all_target_grads_predicted, axis = 0)
    target_grads_predicted_mean = np.mean(all_target_grads_predicted, axis = 0)
    
    print(f"target_grads mae per component: {get_mae(target_grads_ground_truth, target_grads_predicted_mean)}")
    print(f"target_grads rmse per component: {get_rmse(target_grads_ground_truth, target_grads_predicted_mean)}")
    
    if all_target_grads_predicted.shape[0] > 1:
        target_grads_rotational_discrepancies = all_target_grads_predicted - target_grads_predicted_mean[np.newaxis]
        correction = all_target_grads_predicted.shape[0] / (all_target_grads_predicted.shape[0] - 1)
        target_grads_rotational_std = np.sqrt(np.mean(target_grads_rotational_discrepancies ** 2) * correction)
        if args.verbose:
            print(f"target_grads rotational discrepancy std per component: {target_grads_rotational_std} ")
        

if args.verbose:
    print(f"approximate time per atom (batch size is {args.batch_size}): {time_per_atom} seconds")

'''if hypers.USE_DIRECT_TARGETS and not hypers.USE_TARGET_GRADS:
    print(f"approximate time to compute direct_targets per atom: {time_per_atom} seconds")
else:
    print(f"approximate time to compute direct_targets and target_grads per atom: {time_per_atom} seconds")'''
    
    
if args.path_save_predictions is not None:
    if hypers.USE_DIRECT_TARGETS:
        np.save(args.path_save_predictions + '/direct_targets_predicted.npy', direct_targets_predicted_mean)
    if hypers.USE_TARGET_GRADS:
        np.save(args.path_save_predictions + '/target_grads_predicted.npy', target_grads_predicted_mean)
    


