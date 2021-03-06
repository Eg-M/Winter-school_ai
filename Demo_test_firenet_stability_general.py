"""
This script runs the stability test on FIRENET for many different input images. 
It computes perturbations meant to simulate worst-case effect, all of roughly 
the same size.
"""

import time
import yaml

import tensorflow as tf
import numpy as np
import math
import h5py
import scipy.io
from os.path import join 
import os
import shutil

from optimization.gpu.operators import MRIOperator
from optimization.gpu.algorithms import  SR_LASSO_Ergodic, SR_LASSO_exponential
from optimization.utils import generate_weight_matrix
from tfwavelets.dwtcoeffs import get_wavelet
from tfwavelets.nodes import idwt2d
from PIL import Image

from adv_tools_PNAS.automap_config import src_data;
from adv_tools_PNAS.adversarial_tools import l2_norm_of_tensor, cut_to_01, scale_to_01
from adv_tools_PNAS.automap_tools import load_runner
from data_config import read_count
import sys


configfile = 'config_general.yml'
with open(configfile) as ymlfile:
    cgf = yaml.load(ymlfile, Loader=yaml.SafeLoader);

# Set up computational resource 
use_gpu = cgf['COMPUTER_SETUP']['use_gpu']
dtype = eval(cgf['COMPUTER_SETUP']['dtype'])
cdtype = eval(cgf['COMPUTER_SETUP']['cdtype'])
if dtype == tf.float32:
    npdtype = np.float32
elif dtype == tf.float64:
    npdtype = np.float64

print("""\nCOMPUTER SETUP
gpu: {}""".format(use_gpu))
print('PID: ', os.getpid())
if use_gpu:
    compute_node = cgf['COMPUTER_SETUP']['compute_node']
    os.environ["CUDA_VISIBLE_DEVICES"]= "%d" % (compute_node)
    print('Compute node: {}'.format(compute_node))
else: 
    os.environ["CUDA_VISIBLE_DEVICES"]= "-1"

# Turn on soft memory allocation
tf_config = tf.compat.v1.ConfigProto()
tf_config.gpu_options.allow_growth = True
tf_config.log_device_placement = False
sess = tf.compat.v1.Session(config=tf_config)

# Set data parameters
N = cgf['DATA']['N']      # Resolution (N x N image)
srate = cgf['DATA']['srate']  # Resolution (N x N image)
HCP_nbr = cgf['DATA']['HCP_nbr']
im_nbr = cgf['DATA']['im_nbr']
max_norm = float(cgf['DATA']['max_norm'])
runner_id = cgf['DATA']['runner_id']
dest_data = cgf['DATA']['dest_data']
dest_plots = cgf['DATA']['dest_plots']

# Set CS algorithm parameters
lam   = cgf['CS_param']['lam']
tau   = cgf['CS_param']['tau']
sigma = cgf['CS_param']['sigma']
alg_name = cgf['CS_param']['alg_name']
delta = cgf['CS_param']['delta']
initial_x_zero = cgf['CS_param']['initial_x_zero'] # True or false, whether to use x_0 = 0 or x_0 = A^* y
n_iter  = cgf['CS_param']['n_iter']
p_iter  = cgf['CS_param']['p_iter']
wavelet_name = cgf['CS_param']['wavelet_name']
nres = cgf['CS_param']['levels']
db_wavelet = get_wavelet(wavelet_name, dtype)
use_weights = cgf['CS_param']['use_weights'];

# Set parameters for stability algorithm 
stab_lambda = cgf['stability_algorithm']['stab_lambda']
stab_gamma = cgf['stability_algorithm']['stab_gamma']
stab_tau = cgf['stability_algorithm']['stab_tau']
max_num_noise_iter = cgf['stability_algorithm']['max_num_noise_iter']

use_exponential_decay = cgf['stability_algorithm']['use_exponential_decay']

global_step = tf.Variable(0, trainable=False)
if use_exponential_decay:
    stab_start_eta = cgf['stability_algorithm']['expo_config']['stab_start_eta']
    decay_rate     = cgf['stability_algorithm']['expo_config']['decay_rate']
    decay_steps    = cgf['stability_algorithm']['expo_config']['decay_steps']
    staircase      = cgf['stability_algorithm']['expo_config']['staircase']
    learning_rate = tf.compat.v1.train.exponential_decay(stab_start_eta, global_step=global_step, 
                                           decay_steps=decay_steps, decay_rate=decay_rate,
                                           staircase=staircase)
else: 
    learning_rate = cgf['stability_algorithm']['stab_eta']


if not os.path.isdir(dest_data):
    os.mkdir(dest_data);

if not (os.path.isdir(dest_plots)):
    os.mkdir(dest_plots);



samp = np.fft.fftshift(np.asarray(Image.open(join(src_data, 'samp_patt', f"spf2_DAS_N_{N}_srate_{int(100*srate)}_db1.png")))).astype(np.bool)

data = scipy.io.loadmat(join(src_data, 'images', f'image_dataset_{N}.mat')); # im_data 

image_data = data['im_data'];

for i in range(image_data.shape[0]):
    #print('i: ', i);
    image = image_data[i,:,:];
    nim = np.linalg.norm( samp*np.fft.fft2(image)/N,'fro')
    print(f'i: {i}, |y|: {nim}, rel_err_noise (measurements): {max_norm/nim}')

image = image_data[im_nbr, :, :];

L_A = 1;
epsilon_0 = l2_norm_of_tensor(samp*np.fft.fft2(image)/N);

print('p_iter: ', p_iter);
samp = np.expand_dims(samp, -1)

print('samp.shape:', samp.shape, ', Warning: Must be NxN');
if use_weights:
    # Warning: It is very important that mri is an NxN image
    sparsity_levels = cgf['CS_param']['weights_param']['sparsity_levels'] 
    print('Sparsity levelss (coarse to fine): ', sparsity_levels)
    weights = generate_weight_matrix(N, sparsity_levels, npdtype)
    weights = np.expand_dims(weights, -1)
else:
    weights = np.ones([N, N, 1]).astype(npdtype);
print('weights.shape: ', weights.shape);


count = read_count(fname='COUNT_general.txt')

dest_data_full = join(dest_data, f'c{count:03}');
dest_plots_full = join(dest_plots, f'c{count:03}');

if not os.path.isdir(dest_data_full):
    os.mkdir(dest_data_full);
if not os.path.isdir(dest_plots_full):
    os.mkdir(dest_plots_full);

shutil.copyfile(configfile, join(dest_data_full, configfile))
shutil.copyfile(configfile, join(dest_plots_full, configfile))

fileID = open(join(dest_plots_full, 'stability_test_info.txt'), 'a+');


str0 = f'\n\n-------------------- Count: {count:03} ----------------------'
str1 = f'algorithm: {alg_name}\n';
str2 = f'stab_lambda: {stab_lambda}, stab_gamma: {stab_gamma}, stab_tau: {stab_tau}, max_num_noise_iter: {max_num_noise_iter}';
if use_exponential_decay:
    str3 = f'Use exponential_decay: {use_exponential_decay}\n'
    str_extra = f'stab_start_eta: {stab_start_eta}, decay_rate: {decay_rate}, decay_steps: {decay_steps}, staircase: {staircase}'
    str3 = f'{str3}{str_extra}'
else:
    str3 = f'Use exponential_decay: {use_exponential_decay}, learning_rate: {learning_rate}'
if alg_name.lower() ==  'firenet':
    str4 = f'n_iter: {n_iter}, p_iter: {p_iter}, eps_0: {epsilon_0}, lambda: {lam}, sigma: {sigma}, tau: {tau}, wname: {wavelet_name}, nres: {nres}, use_weights: {use_weights}';
    if use_weights:
        str4 += ", sparsity_levels: %s" % (cgf['CS_param']['weights_param']['sparsity_levels'])
else:
    str4 = f'n_iter: {n_iter}, lambda: {lam}, sigma: {sigma}, tau: {tau}, wname: {wavelet_name}, nres: {nres}, use_weights: {use_weights}'
    if use_weights:
        str4 += ", sparsity_levels: %s" % (cgf['CS_param']['weights_param']['sparsity_levels'])
    
    if initial_x_zero:
        str4 += ', x0 = 0'
    else:
        str4 += ', x0 = A^* y'

fileID.write(str0 + '\n')
fileID.write(str1 + '\n')
fileID.write(str2 + '\n')
fileID.write(str3 + '\n')
fileID.write(str4 + '\n')

print(str0)
print(str1)
print(str2)
print(str3)
print(str4)

############################################################################
###                     Build the Tensorflow graph                       ### 
############################################################################

# The lambda in the objective function for generating adv. noise
pl_noise_penalty = tf.compat.v1.placeholder(dtype, shape=(), name='noise_penalty')

# Parameters for CS algorithm
tf_L_A = tf.constant( 1.0, dtype, shape=(), name='L_A');
pl_sigma = tf.compat.v1.placeholder(dtype, shape=(), name='sigma')
pl_tau   = tf.compat.v1.placeholder(dtype, shape=(), name='tau')
pl_lam   = tf.compat.v1.placeholder(dtype, shape=(), name='lambda')
pl_n_iter = tf.compat.v1.placeholder(tf.int32, shape=(), name='n_iter')
pl_p_iter = tf.compat.v1.placeholder(tf.int32, shape=(), name='p_iter')
pl_eps_0 = tf.compat.v1.placeholder(dtype, shape=(), name='eps_0');
pl_delta = tf.compat.v1.placeholder(dtype, shape=(), name='delta');
# For the weighted l^1-norm
pl_weights = tf.compat.v1.placeholder(dtype, shape=[N,N,1], name='weights')

# Build Primal-dual graph
tf_im = tf.compat.v1.placeholder(cdtype, shape=[N,N,1], name='image')
tf_samp_patt = tf.compat.v1.placeholder(tf.bool, shape=[N,N,1], name='sampling_pattern')

# perturbation
tf_rr_real = tf.Variable(stab_tau*tf.random.uniform(tf_im.shape, dtype=dtype), name='rr_real', trainable=True)
tf_rr_imag = tf.Variable(stab_tau*tf.random.uniform(tf_im.shape, dtype=dtype), name='rr_imag', trainable=True)

tf_rr = tf.complex(tf_rr_real, tf_rr_imag, name='rr')

tf_input = tf_im + tf_rr

op = MRIOperator(tf_samp_patt, db_wavelet, nres, dtype=dtype)
tf_measurements = op.sample(tf_input)

tf_adjoint_coeffs = op(tf_measurements, adjoint=True)
adj_real_idwt = idwt2d(tf.math.real(tf_adjoint_coeffs), db_wavelet, nres)
adj_imag_idwt = idwt2d(tf.math.imag(tf_adjoint_coeffs), db_wavelet, nres)
tf_adjoint = tf.complex(adj_real_idwt, adj_imag_idwt)

if initial_x_zero:
    print("x_0 = 0")
    tf_initial_x = tf.zeros_like(tf_input)
else:
    tf_initial_x = op(tf_measurements, adjoint=True)
    print('x_0 = A^* y')

if alg_name.lower() ==  'firenet':
    alg = SR_LASSO_exponential(tf_measurements, tf_initial_x, op, p_iter=pl_p_iter, 
                               tau=pl_tau, sigma=pl_sigma, lam=pl_lam, 
                               weights_mat=pl_weights, L_A=tf_L_A, 
                               eps_0=pl_eps_0, delta=pl_delta,
                               dtype=dtype)
    result_coeffs = alg.run(n_iter=pl_n_iter)
elif alg_name.lower() == 'sr_lasso_ergodic': 
    alg = SR_LASSO_Ergodic(op, p_iter=pl_n_iter, tau=pl_tau, sigma=pl_sigma, 
                           lam=pl_lam, weights_mat=pl_weights, dtype=dtype) # Obs. notice mixup between n_iter and p_iter.
    result_coeffs = alg.run(tf_measurements, tf_initial_x)
else:
    print(f"alg_name: {alg_name} not recognized");

real_idwt = idwt2d(tf.math.real(result_coeffs), db_wavelet, nres)
imag_idwt = idwt2d(tf.math.imag(result_coeffs), db_wavelet, nres)
tf_recovery = tf.complex(real_idwt, imag_idwt)

tf_solution = tf.compat.v1.placeholder(cdtype, shape=[N,N,1], name='actual')

tf_obj = tf.nn.l2_loss(tf.abs(tf_recovery - tf_solution)) - pl_noise_penalty * tf.nn.l2_loss(tf.abs(tf_rr))
# End building objective function for adv noise

opt = tf.compat.v1.train.MomentumOptimizer(learning_rate, stab_gamma, use_nesterov=True).minimize(
        -tf_obj, var_list=[tf_rr_real, tf_rr_imag], global_step=global_step)

tf_rr_concat = tf.concat([tf_rr_real, tf_rr_imag], axis=-1) 


start = time.time()
with tf.compat.v1.Session() as sess:
    for im_nbr in range(image_data.shape[0]):
        print('im_nbr: ', im_nbr)

        image = image_data[im_nbr,:,:];
        image = np.expand_dims(image, -1)

        sess.run(tf.compat.v1.global_variables_initializer())
    
        print('Global initilization done')
    
        noiseless = sess.run(tf_recovery, feed_dict={ 'tau:0': tau,
                                                      'lambda:0': lam,
                                                      'sigma:0': sigma,
                                                      'weights:0': weights,
                                                      'n_iter:0': n_iter,
                                                      'p_iter:0': p_iter,
                                                      'image:0': image,
                                                      'eps_0:0': epsilon_0,
                                                      'delta:0': delta,
                                                      'sampling_pattern:0': samp,
            })

        print('Computed noiseless reconstruction')

        noiseless_sq = np.squeeze(noiseless);
        scipy.io.savemat(join(dest_data_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_noiseless.mat'), 
                {'image': image, 'image_rec': noiseless})

        fname_out_rec  = join(dest_plots_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_rec_noiseless.png'); 
        fname_out_orig = join(dest_plots_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_orig_noiseless.png'); 

        Image_im_rec_noiseless = Image.fromarray(np.uint8(255*np.abs(np.squeeze(noiseless))));
        Image_im_orig = Image.fromarray(np.uint8(255*np.abs(np.squeeze(image))));

        Image_im_rec_noiseless.save(fname_out_rec);
        Image_im_orig.save(fname_out_orig);
        print('Saved images')

        i = 1;
        length = -1;
        while(i < (max_num_noise_iter+1) and length < max_norm):

            #print('{pert_nbr}: {i}/{num}. Time (min): {t}'.format(pert_nbr=pert_nbr, i=i, num=max_num_noise_iter, t=(time.time()-start)/60))

            sess.run(opt, feed_dict={'image:0': image,
                                     'sampling_pattern:0': samp,
                                     'sigma:0': sigma,
                                     'tau:0': tau,
                                     'lambda:0': lam,
                                     'eps_0:0': epsilon_0,
                                     'delta:0': delta,
                                     'n_iter:0': n_iter,
                                     'p_iter:0': p_iter,
                                     'noise_penalty:0': stab_lambda,
                                     'weights:0': weights,
                                     'actual:0': noiseless})


            rr = sess.run(tf.complex(tf_rr_real, tf_rr_imag))

            im_adjoint = sess.run(tf_adjoint, feed_dict={'image:0': image,
                                                             'sampling_pattern:0': samp,
                                                             'sigma:0': sigma,
                                                             'lambda:0': lam,
                                                             'tau:0': tau,
                                                             'n_iter:0': n_iter,
                                                             'p_iter:0': p_iter,
                                                             'noise_penalty:0': stab_lambda,
                                                             'weights:0': weights,
                                                             'actual:0': noiseless})

            im_rec = sess.run(tf_recovery, feed_dict={'image:0': image,
                                                         'sampling_pattern:0': samp,
                                                         'sigma:0': sigma,
                                                         'tau:0': tau,
                                                         'eps_0:0': epsilon_0,
                                                         'delta:0': delta,
                                                         'n_iter:0': n_iter,
                                                         'p_iter:0': p_iter,
                                                         'weights:0': weights,
                                                         'noise_penalty:0': stab_lambda,
                                                         'lambda:0': lam,
                                                         'actual:0': noiseless})

            rr_save = np.squeeze(rr);
            im_adjoint = np.squeeze(im_adjoint);
            im_rec = np.squeeze(im_rec);
            im_orig_p_noise = np.squeeze(image+rr);

            diff_mapping = l2_norm_of_tensor(noiseless_sq - im_rec);
            length = l2_norm_of_tensor(rr)
            t = (time.time()-start)/60
            print(f'{i:3}/{max_num_noise_iter}, im_nbr: {im_nbr}, Norm: {length:10g} / {max_norm:10g}, time (min): {t:.2f}, |f(Ax) - f(A(x+r))|: {diff_mapping}')

            fileID.write(f'itr: {i}, im_nbr: {im_nbr}, norm: {length}, target norm: {max_norm}, |f(Ax) - f(A(x+r))|: {diff_mapping}\n')

            # Start old while loop.
            if length > max_norm or i == max_num_noise_iter:
                scipy.io.savemat(join(dest_data_full, f'im_im_nbr_{im_nbr}_experi_{count:03}.mat'), 
                        {'image': image, 'image_rec': im_rec, 'rr': rr_save, 'im_adjoint': im_adjoint})

                fname_out_rec = join(dest_plots_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_rec.png')
                fname_out_adjoint = join(dest_plots_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_adjoint.png')
                fname_out_orig_p_noise = join(dest_plots_full, f'im_im_nbr_{im_nbr}_experi_{count:03}_orig_p_noise.png')

                im_rec = cut_to_01(np.abs(im_rec));
                im_adjoint = cut_to_01(np.abs(im_adjoint));
                im_orig_p_noise = cut_to_01(np.abs(im_orig_p_noise));

                Image_im_rec = Image.fromarray(np.uint8(255*im_rec));
                Image_im_adjoint = Image.fromarray(np.uint8(255*im_adjoint));
                Image_im_orig_p_noise = Image.fromarray(np.uint8(255*im_orig_p_noise));

                Image_im_rec.save(fname_out_rec);
                Image_im_adjoint.save(fname_out_adjoint);
                Image_im_orig_p_noise.save(fname_out_orig_p_noise);

            i += 1

    print('We have reached the end of the session')
print('Program have reached the end')




