import os, sys, inspect
sys.path.insert(1, os.path.join(sys.path[0], '../../'))
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
from utils import *
import seaborn as sns
from core.uniform_concentration import *
from core.pfdr import *
from lambda_vs_pfdr import get_lambdas_vs_pfdps_frac_predict
import pdb

def plot_histograms(df_list,alpha,delta,pfdps,frac_predict,lambdas):
    fig, axs = plt.subplots(nrows=1,ncols=2,figsize=(12,3))

    axs[1].plot(lambdas,pfdps,color='k',linewidth=3,label='pFDP')
    axs[1].plot(lambdas,frac_predict,color='g',linewidth=3,label='avg size')

    pfdps = []
    labels = []
    for i in range(len(df_list)):
        df = df_list[i]
        if df.pFDP.sum() <= 1e-3:
            continue
        region_name = df["region name"][0]
        if region_name == "Multiplier Bootstrap":
            region_name = "Multiplier\nBootstrap"
        if region_name == "Fixed Sequence (Multi-Start)":
            region_name = "Fixed Sequence\n(Multi-Start)"
        pfdps = pfdps + [np.array(df['pFDP'].tolist()),]
        labels = labels + [region_name,]

    sns.violinplot(data=pfdps, ax=axs[0], orient='h', inner=None)
    
    axs[0].set_xlabel('pFDP')
    axs[0].locator_params(axis='x', nbins=4)
    axs[0].axvline(x=alpha,c='#999999',linestyle='--',alpha=0.7)
    axs[0].set_yticklabels(labels)
    axs[1].set_xlabel(r'$\lambda$')
    axs[1].legend()
    sns.despine(ax=axs[0],top=True,right=True)
    sns.despine(ax=axs[1],top=True,right=True)
    plt.tight_layout()
    plt.savefig((f'outputs/histograms/pfdp_{alpha}_{delta}_imagenet_histograms').replace('.','_') + '.pdf')

def trial_precomputed(rejection_region_function, top_scores, corrects, alpha, delta, lambdas, num_calib, maxiter):
    total=top_scores.shape[0]
    m=1000
    perm = torch.randperm(total)
    top_scores = top_scores[perm]
    corrects = corrects[perm].float()
    calib_scores, val_scores = (top_scores[0:num_calib], top_scores[num_calib:])
    calib_corrects, val_corrects = (corrects[0:num_calib], corrects[num_calib:])

    calib_scores, indexes = calib_scores.sort()
    calib_corrects = calib_corrects[indexes] 
    calib_accuracy = (calib_corrects.flip(dims=(0,)).cumsum(dim=0)/(torch.tensor(range(num_calib))+1)).flip(dims=(0,))
    calib_abstention_freq = (torch.tensor(range(num_calib))+1).float().flip(dims=(0,))/num_calib

    R = rejection_region_function(calib_scores.numpy(), calib_corrects.numpy(), lambdas, alpha, delta)

    if R.shape[0] == 0:
        return 0.0, 0.0, 1.0

    lhat = lambdas[R.min()]

    val_predictions = val_scores > lhat

    pfdp = 1-val_corrects[val_predictions].float().mean()
    pfdp = np.nan_to_num(pfdp)
    
    mean_size = val_predictions.float().mean()
    
    return pfdp, mean_size, lhat

def experiment(alpha,delta,lambdas,num_calib,num_trials,maxiter,imagenet_val_dir):
    df_list = []
    def monotonic_pfdr_bonferroni_search_HB(score_vector, correct_vector, lambdas, alpha, delta):
        return pfdr_bonferroni_search_HB(score_vector, correct_vector, lambdas, alpha, delta,downsample_factor=lambdas.shape[0])
    rejection_region_functions = (pfdr_uniform, pfdr_bonferroni_HB, pfdr_bonferroni_search_HB, monotonic_pfdr_bonferroni_search_HB, pfdr_romano_wolf_multiplier_bootstrap) 
    rejection_region_names = ("Uniform","Bonferroni",'Fixed Sequence\n(Multi-Start)',"Fixed Sequence",'Multiplier\nBootstrap')

    for idx in range(len(rejection_region_functions)):
        rejection_region_function = rejection_region_functions[idx]
        rejection_region_name = rejection_region_names[idx]
        fname = f'./.cache/{alpha}_{delta}_{num_calib}_{num_trials}_{rejection_region_name}_dataframe.pkl'

        df = pd.DataFrame(columns = ["$\\hat{\\lambda}$","pFDP","mean size","alpha","delta","region name"])
        try:
            df = pd.read_pickle(fname)
        except FileNotFoundError:
            dataset_precomputed = get_logits_dataset('ResNet152', 'Imagenet', imagenet_val_dir)
            print('Dataset loaded')
            
            classes_array = get_imagenet_classes()
            T = platt_logits(dataset_precomputed)
            
            logits, labels = dataset_precomputed.tensors
            top_scores, top_classes = (logits/T.cpu()).softmax(dim=1).max(dim=1)
            corrects = top_classes==labels

            #if rejection_region_name == "HBBonferroniSearch_J=1":
            #    pdb.set_trace()

            with torch.no_grad():
                local_df_list = []
                for i in tqdm(range(num_trials)):
                    pfdp, mean_size, lhat = trial_precomputed(rejection_region_function, top_scores, corrects, alpha, delta, lambdas, num_calib, maxiter)
                    dict_local = {"$\\hat{\\lambda}$": lhat,
                                    "pFDP": pfdp,
                                    "mean size": mean_size,
                                    "alpha": alpha,
                                    "delta": delta,
                                    "index": [0],
                                    "region name": rejection_region_name,
                                 }
                    df_local = pd.DataFrame(dict_local)
                    local_df_list = local_df_list + [df_local]
                df = pd.concat(local_df_list, axis=0, ignore_index=True)
                df.to_pickle(fname)

        df_list = df_list + [df]
    pfdps, frac_predict = get_lambdas_vs_pfdps_frac_predict(lambdas,imagenet_val_dir)
    plot_histograms(df_list, alpha, delta, pfdps, frac_predict, lambdas)

def platt_logits(calib_dataset, max_iters=10, lr=0.01, epsilon=0.01):
    calib_loader = torch.utils.data.DataLoader(calib_dataset, batch_size=1024, shuffle=False, pin_memory=True) 
    nll_criterion = nn.CrossEntropyLoss().cuda()

    T = nn.Parameter(torch.Tensor([1.3]).cuda())

    optimizer = optim.SGD([T], lr=lr)
    for iter in range(max_iters):
        T_old = T.item()
        for x, targets in calib_loader:
            optimizer.zero_grad()
            x = x.cuda()
            x.requires_grad = True
            out = x/T
            loss = nll_criterion(out, targets.long().cuda())
            loss.backward()
            optimizer.step()
        if abs(T_old - T.item()) < epsilon:
            break
    return T 

if __name__ == "__main__":
    sns.set(palette='pastel',font='serif')
    sns.set_style('white')
    fix_randomness(seed=0)

    imagenet_val_dir = '/scratch/group/ilsvrc/val' #TODO: Replace this with YOUR location of imagenet val set.

    alphas = [0.15,0.1,0.05]
    deltas = [0.1,0.1,0.1]
    params = list(zip(alphas,deltas))
    maxiter = int(1e3)
    num_trials = 100 
    num_calib = 30000
    lambdas = np.linspace(0,1,1000)
    
    for alpha, delta in params:
        print(f"\n\n\n ============           NEW EXPERIMENT alpha={alpha} delta={delta}           ============ \n\n\n") 
        experiment(alpha,delta,lambdas,num_calib,num_trials,maxiter,imagenet_val_dir)
