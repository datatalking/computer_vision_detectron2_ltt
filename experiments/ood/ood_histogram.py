import os, sys, inspect
sys.path.insert(1, os.path.join(sys.path[0], '../../'))
import ctypes
import torch
import torchvision as tv
import argparse
import time
import numpy as np
from scipy.stats import binom
from PIL import Image
import matplotlib
import matplotlib.pyplot as plt
import pandas as pd
import pickle as pkl
from tqdm import tqdm
import seaborn as sns
from utils import *
from core.bounds import hb_p_value
from core.concentration import *
from statsmodels.stats.multitest import multipletests
import pdb
import multiprocessing as mp
import time

data = {}
global_dict = {"loss_tables": None}

def plot_histograms(df_list,alphas,delta):
    fig, axs = plt.subplots(nrows=1,ncols=3,figsize=(12,3))
    for df in df_list:
        region_name = df['region name'][0]
        axs[0].hist(df['coverage'], alpha=0.7)
        axs[1].hist(df['OOD Type I'], alpha=0.7)
        axs[2].scatter(1-df['coverage'],df['OOD Type I'], alpha=0.7, label=region_name)
        fraction_violated = ((df['coverage'] < 1-alphas[1]) | (df['OOD Type I'] > alphas[0])).astype(float).mean()
        print(f"Fraction violated (at least one risk) using {region_name}: {fraction_violated}")
    # Limits, lines, and labels
    axs[0].set_ylabel("Histogram Density")
    axs[0].set_xlabel("Coverage")
    axs[0].axvline(x=1-alphas[1],c='#999999',linestyle='--',alpha=0.7)
    axs[0].locator_params(axis="x", nbins=4)
    axs[0].locator_params(axis="y", nbins=4)
    axs[1].set_xlabel("CIFAR marked OOD")
    axs[1].axvline(x=alphas[0],c='#999999',linestyle='--',alpha=0.7)
    axs[1].locator_params(axis="x", nbins=4)
    axs[1].locator_params(axis="y", nbins=4)
    axs[2].axvline(x=alphas[1],c='#999999', linestyle='--', alpha=0.7)
    axs[2].axhline(y=alphas[0],c='#999999', linestyle='--', alpha=0.7)
    axs[2].legend(loc='lower left')
    axs[2].set_xlim(left=0,right=1.05*max([(1-df['coverage']).max() for df in df_list]))
    axs[2].set_ylim(bottom=0,top=1.05*max([df['OOD Type I'].max() for df in df_list]))
    axs[2].set_xlabel("CIFAR marked OOD")
    axs[2].set_ylabel("Miscoverage")
    axs[2].locator_params(axis="x", nbins=4)
    axs[2].locator_params(axis="y", nbins=4)
    sns.despine(ax=axs[0],top=True,right=True)
    sns.despine(ax=axs[1],top=True,right=True)
    sns.despine(ax=axs[2],top=True,right=True)
    fig.tight_layout()
    os.makedirs("./outputs/histograms",exist_ok=True)
    plt.savefig("./" + f"outputs/histograms/ood_{alphas[0]}_{alphas[1]}_{delta}_histograms".replace(".","_") + ".pdf")

# Table will be n x m x N x N, where n is number of samples, m is number of losses, and N is sampling of lambda
def get_loss_tables(data,lambda1s,lambda2s):
    os.makedirs('./.cache/', exist_ok=True)
    try:
        loss_tables = torch.load('./.cache/loss_tables.pt')
        size_table = torch.load('./.cache/size_table.pt')
        frac_ind_ood_table = torch.load('./.cache/frac_ind_ood_table.pt')
        frac_ood_ood_table = torch.load('./.cache/frac_ood_ood_table.pt')
    except FileNotFoundError:
        # Load data
        odin_ind = data['odin_ind']
        odin_ind, ind_sort = odin_ind.sort()
        odin_ood = data['odin_ood']
        odin_ood, ood_sort = odin_ood.sort()
        softmax_ind = data['softmax_ind'][ind_sort]
        softmax_ood = data['softmax_ood'][ood_sort]
        labels_ind = data['labels_ind'][ind_sort]
        labels_ood = data['labels_ood'][ood_sort]
        # Preallocate space
        loss_tables = torch.zeros((softmax_ind.shape[0],2,lambda1s.shape[0],lambda2s.shape[0]))
        size_table = torch.zeros((softmax_ind.shape[0],lambda1s.shape[0],lambda2s.shape[0]))
        frac_ind_ood_table = torch.zeros((lambda1s.shape[0],))
        frac_ood_ood_table = torch.zeros((lambda1s.shape[0],))
        print("Calculating loss tables.")
        for i in tqdm(range(lambda1s.shape[0])):
            num_incorrect_ind = (odin_ind > lambda1s[i]).float().sum()
            num_incorrect_ood = (odin_ood <= lambda1s[i]).float().sum()
            frac_ind_ood_table[i] = num_incorrect_ind/float(odin_ind.shape[0])
            frac_ood_ood_table[i] = 1-num_incorrect_ood/float(odin_ind.shape[0])
            if i > 0 and frac_ind_ood_table[i] == frac_ind_ood_table[i-1]:
                loss_tables[:,:,i,:] = loss_tables[:,:,i-1,:]
                size_table[:,i,:] = size_table[:,i-1,:]
            else:
                for j in range(lambda2s.shape[0]):
                    if num_incorrect_ind == 0:
                        index_split = None
                    else:
                        index_split = -int(num_incorrect_ind)
                    _softmax_ind = softmax_ind[:index_split] 
                    if _softmax_ind.shape[0] > 0:
                        srtd, pi = _softmax_ind.sort(dim=1,descending=True)
                        sizes = (srtd.cumsum(dim=1) <= lambda2s[j]).int().sum(dim=1)
                        sizes = torch.max(sizes,torch.ones_like(sizes))
                        rank_of_true = (pi == labels_ind[:index_split,None]).int().argmax(dim=1) + 1
                        missed = ( sizes < rank_of_true ).int()
                        loss_tables[:index_split,1,i,j] = missed 
                        size_table[:index_split,i,j] = sizes
                loss_tables[:,0,i,:] = (odin_ind > lambda1s[i]).int().unsqueeze(dim=1)
            print(f"\n\ri: {i}, Frac InD OOD: {frac_ind_ood_table[i]}, Frac OOD OOD: {frac_ood_ood_table[i]}\033[1A",end="")
        torch.save(loss_tables,"./.cache/loss_tables.pt")
        torch.save(size_table,"./.cache/size_table.pt")
        torch.save(frac_ind_ood_table,"./.cache/frac_ind_ood_table.pt")
        torch.save(frac_ood_ood_table,"./.cache/frac_ood_ood_table.pt")
        print("Loss tables calculated!")

    return loss_tables, size_table, frac_ind_ood_table, frac_ood_ood_table

def calculate_corrected_p_values(calib_tables, alphas, lambda1s, lambda2s):
    n = calib_tables.shape[0]
    # Get p-values for each loss
    r_hats_risk1 = calib_tables[:,0,:].mean(axis=0).squeeze().flatten() # empirical risk at each lambda combination
    p_values_risk1 = np.array([hb_p_value(r_hat,n,alphas[0]) for r_hat in r_hats_risk1])
    r_hats_risk2 = (calib_tables[:,1,:] * (1-calib_tables[:,0,:]) - alphas[1]*(1-calib_tables[:,0,:])).mean(axis=0).squeeze().flatten() + alphas[1] # empirical risk at each lambda combination using trick
    p_values_risk2 = np.array([hb_p_value(r_hat,n,alphas[1]) for r_hat in r_hats_risk2])

    # Combine them
    p_values_corrected = np.maximum(p_values_risk1,p_values_risk2) 
    return p_values_corrected
    
def flatten_lambda_meshgrid(lambda1s,lambda2s):
    l1_meshgrid, l2_meshgrid = torch.meshgrid(torch.tensor(lambda1s),torch.tensor(lambda2s))
    l1_meshgrid = l1_meshgrid.flatten()
    l2_meshgrid = l2_meshgrid.flatten()
    return l1_meshgrid, l2_meshgrid

def trial_precomputed(method_name, alphas, delta, lambda1s, lambda2s, num_calib, maxiter, i, r1, r2, oodt2, lht, curr_proc_dict):
    n = global_dict['loss_tables'].shape[0]
    fix_randomness(seed = i * curr_proc_dict['num']) 
    perm = torch.randperm(n)
    
    loss_tables = global_dict['loss_tables'][perm]
    calib_tables, val_tables = (loss_tables[:num_calib], loss_tables[num_calib:])
    l1_meshgrid, l2_meshgrid = flatten_lambda_meshgrid(lambda1s,lambda2s)
    lambda_selector = np.ones((lambda1s.shape[0]*lambda2s.shape[0],)) > 2  # All false
    
    if method_name == "Multiscale HBBonferroni":
        n_coarse = int(calib_tables.shape[0]/10)
        coarse_tables, fine_tables = (calib_tables[:n_coarse], calib_tables[n_coarse:])
        p_values_coarse = calculate_corrected_p_values(coarse_tables, alphas, lambda1s, lambda2s)
        # Get a band around delta that contains about 5% of examples.
        delta_quantile = (p_values_coarse <= delta).mean()
        lambda_selector[p_values_coarse <= 1.5*delta] = True
        frac_selected = lambda_selector.astype(float).mean()
        if frac_selected == 0:
            print("Selection failed!")
            lambda_selector[:] = True 
        else:
            p_values_corrected = calculate_corrected_p_values(fine_tables, alphas, lambda1s, lambda2s)
    else:
        p_values_corrected = calculate_corrected_p_values(calib_tables, alphas, lambda1s, lambda2s)
        lambda_selector[:] = True

    if method_name == "HBBFSearch":
        #p_values_corrected = p_values_corrected.reshape((lambda1s.shape[0],lambda2s.shape[0]))
        R = np.nonzero(p_values_corrected < (delta / lambda1s.shape[0]))[0]
    else:
        # Bonferroni correct over lambda to get the valid discoveries
        R = bonferroni(p_values_corrected[lambda_selector], delta)

    if R.shape[0] == 0:
        return 0.0, 0.0, 0.0, np.array([1.0,1.0]) 

    # Index the lambdas
    l1_meshgrid = l1_meshgrid[lambda_selector]
    l2_meshgrid = l2_meshgrid[lambda_selector]
    l1s = l1_meshgrid[R]
    l2s = l2_meshgrid[R]

    lhat = np.array([l1s.min(), l2s[l1s == l1s.min()].min()])

    # Validate
    idx1 = np.nonzero(np.abs(lambda1s-lhat[0]) < 1e-6)[0]
    idx2 = np.nonzero(np.abs(lambda2s-lhat[1]) < 1e-6)[0] 

    num_ood = val_tables[:,0,idx1,idx2].sum()
    risk1 = float(num_ood) / float(val_tables.shape[0])
    selector = -int(num_ood) if num_ood != 0 else None
    risk2 = val_tables[:selector,1,idx1,idx2].mean().item()
    
    ood_type2 = 1-global_dict['frac_ood_ood_table'][idx1].item()
    
    r1[i] = risk1
    r2[i] = risk2
    oodt2[i] = ood_type2
    lht[i] = lhat
    curr_proc_dict['num'] -= 1

# Define the tables in the global scope

def experiment(alphas,delta,lambda1s,lambda2s,num_calib,num_trials,maxiter,cache_dir):
    df_list = []
    rejection_region_names = ("HBBonferroni","HBBFSearch")

    for idx in range(len(rejection_region_names)):
        rejection_region_name = rejection_region_names[idx]
        fname = f'./.cache/{alphas}_{delta}_{num_calib}_{num_trials}_{rejection_region_name}_dataframe.pkl'

        df = pd.DataFrame(columns = ["$\\hat{\\lambda}$","coverage","OOD Type I","OOD Type II","alpha1","alpha2","delta","region name"])
        try:
            df = pd.read_pickle(fname)
        except FileNotFoundError:
            data['softmax_ind'] = torch.load(cache_dir + "softmax_scores_in_distribution.pt")
            data['softmax_ood'] = torch.load(cache_dir + "softmax_scores_out_of_distribution.pt")
            data['odin_ind'] = 1-torch.load(cache_dir + "ood_scores_in_distribution.pt")
            data['odin_ood'] = 1-torch.load(cache_dir + "ood_scores_out_of_distribution.pt")
            data['labels_ind'] = torch.load(cache_dir + "labels_in_distribution.pt")
            data['labels_ood'] = torch.load(cache_dir + "labels_out_of_distribution.pt")
            print('Dataset loaded')
            
            lambda1s = np.linspace(np.quantile(data['odin_ind'],0.5),np.quantile(data['odin_ind'],1-alphas[0]),100) 

            # Load data 
            global_dict['loss_tables'], global_dict['size_table'], global_dict['frac_ind_ood_table'], global_dict['frac_ood_ood_table'] = get_loss_tables(data,lambda1s,lambda2s)

            with torch.no_grad():
                # Setup shared memory for experiments
                manager = mp.Manager()
                return_risk1 = manager.dict({ k:0. for k in range(num_trials)})
                return_risk2 = manager.dict({ k:0. for k in range(num_trials)})
                return_ood_type2 = manager.dict({ k:0. for k in range(num_trials)})
                return_lhat = manager.dict({ k:np.array([]) for k in range(num_trials)})
                curr_proc_dict = manager.dict({'num': 0})

                # Multiprocessing: https://stackoverflow.com/questions/7894791/use-numpy-array-in-shared-memory-for-multiprocessing and https://docs.python.org/3/library/multiprocessing.shared_memory.html

                jobs = []

                for i in range(num_trials):
                    p = mp.Process(target=trial_precomputed, args=(rejection_region_name, alphas, delta, lambda1s, lambda2s, num_calib, maxiter, i, return_risk1, return_risk2, return_ood_type2, return_lhat, curr_proc_dict))
                    jobs.append(p)

                pbar = tqdm(total=num_trials)

                for proc in jobs:
                    while curr_proc_dict['num'] >= 30:
                        time.sleep(2)
                    proc.start()
                    curr_proc_dict['num'] += 1
                    pbar.update(1)

                pbar.close()

                for proc in jobs:
                    proc.join()

                # Form the large dataframe
                local_df_list = []
                for i in tqdm(range(num_trials)):
                    dict_local = {"$\\hat{\\lambda}$": [return_lhat[i],],
                                    "coverage": 1-return_risk2[i],
                                    "OOD Type I": return_risk1[i],
                                    "OOD Type II": return_ood_type2[i],
                                    "alpha1": alphas[0],
                                    "alpha2": alphas[1],
                                    "delta": delta,
                                    "index": [0],
                                    "region name": rejection_region_name,
                                 }
                    df_local = pd.DataFrame(dict_local)
                    local_df_list = local_df_list + [df_local]
                df = pd.concat(local_df_list, axis=0, ignore_index=True)
                df.to_pickle(fname)

        df_list = df_list + [df]
    plot_histograms(df_list,alphas,delta)

if __name__ == "__main__":
    sns.set(palette='pastel',font='serif')
    sns.set_style('white')
    fix_randomness(seed=0)
    mp.set_start_method('fork') 

    cache_dir = './odin/code/.cache/' 

    alphas = [0.05,0.01]
    delta = 0.1
    maxiter = int(1e3)
    num_trials = 1000 
    num_calib = 8000
    lambda1s = None 
    lambda2s = np.linspace(0,1,1000)
    
    experiment(alphas,delta,lambda1s,lambda2s,num_calib,num_trials,maxiter,cache_dir)
