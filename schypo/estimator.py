"""
	estimator.py
	
	Contains the estimators for the mean, variance, covariance, and the correlation. 
	
	Implements both the approximate hypergeometric and Poisson generative models. 
	
	If :data: is a tuple, it is assumed that it is a tuple of (value, count), both of which are 1D numpy arrays.
	If :data: is not a tuple, it is assumed to be a SciPy CSC sparse matrix.
"""

import scipy.sparse as sparse
import scipy.stats as stats
import numpy as np
import time
import scipy as sp
import matplotlib.pyplot as plt
from sklearn import linear_model


def _get_estimator_1d(estimator_type):
	
	if estimator_type == 'hyper_absolute':
		return _hyper_1d_absolute
	elif estimator_type == 'hyper_relative':
		return _hyper_1d_relative
	elif estimator_type == 'poi_absolute':
		return _poi_1d_absolute
	elif estimator_type == 'poi_relative':
		return _poi_1d_relative
	else: # Custom 1D estimator
		return estimator_type[0]

	
def _get_estimator_cov(estimator_type):
	
	if estimator_type == 'hyper_absolute':
		return _hyper_cov_absolute
	elif estimator_type == 'hyper_relative':
		return _hyper_cov_relative
	elif estimator_type == 'poi_absolute':
		return _poi_cov_absolute
	elif estimator_type == 'poi_relative':
		return _poi_cov_relative
	else: # Custom covariance estimator
		return estimator_type[1]
	

def _estimate_size_factor(data, estimator_type):
	"""Calculate the size factor
	
	Args: 
		data (AnnData): the scRNA-Seq CG (cell-gene) matrix.
		
	Returns:
		size_factor((Nc,) ndarray): the cell size factors.
	"""
	
	if 'absolute' in estimator_type:
		return np.ones(data.shape[0])
	
	X=data
	Nrc = np.array(X.sum(axis=1)).reshape(-1)
	Nr = Nrc.mean()
	size_factor = Nrc
	return size_factor


def _fit_mv_regressor(mean, var):
	"""
		Perform linear regression of the variance against the mean.
		
		Uses the RANSAC algorithm to choose a set of genes to perform 1D linear regression.
	"""
	
	cond = (mean > 0) & (var > 0)
	m, v = np.log(mean[cond]), np.log(var[cond])
	slope, inter, _, _, _ = stats.linregress(m, v)
	
	return [slope, inter]


def _residual_variance(mean, var, mv_fit):
	
	cond = (mean > 0)
	rv = np.zeros(mean.shape)*np.nan
	rv[cond] = var[cond] / (np.exp(mv_fit[1])*mean[cond]**mv_fit[0])
# 	rv[cond] = var[cond]/mean[cond]
	
	return rv


def _poisson_1d_relative(data, n_obs, size_factor=None):
	"""
		Estimate the variance using the Poisson noise process.
		
		If :data: is a tuple, :cell_size: should be a tuple of (inv_sf, inv_sf_sq). Otherwise, it should be an array of length data.shape[0].
	"""
	if type(data) == tuple:
		size_factor = size_factor if size_factor is not None else (1, 1)
		mm_M1 = (data[0]*data[1]*size_factor[0]).sum(axis=0)/n_obs
		mm_M2 = (data[0]**2*data[1]*size_factor[1] - data[0]*data[1]*size_factor[1]).sum(axis=0)/n_obs
	else:
		
		row_weight = (1/size_factor).reshape([1, -1]) if size_factor is not None else np.ones(data.shape[0])
		mm_M1 = sparse.csc_matrix.dot(row_weight, data).ravel()/n_obs
		mm_M2 = sparse.csc_matrix.dot(row_weight**2, data.power(2)).ravel()/n_obs - sparse.csc_matrix.dot(row_weight**2, data).ravel()/n_obs
	
	mm_mean = mm_M1
	mm_var = (mm_M2 - mm_M1**2)

	return [mm_mean, mm_var]


def _poisson_cov_relative(data, n_obs, size_factor, idx1=None, idx2=None):
	"""
		Estimate the covariance using the Poisson noise process between genes at idx1 and idx2.
		
		If :data: is a tuple, :cell_size: should be a tuple of (inv_sf, inv_sf_sq). Otherwise, it should be an array of length data.shape[0].
	"""
	
	if type(data) == tuple:
		obs_M1 = (data[0]*data[2]*size_factor[0]).sum(axis=0)/n_obs
		obs_M2 = (data[1]*data[2]*size_factor[0]).sum(axis=0)/n_obs
		obs_MX = (data[0]*data[1]*data[2]*size_factor[1]).sum(axis=0)/n_obs
		cov = obs_MX - obs_M1*obs_M2

	else:

		idx1 = np.arange(0, data.shape[1]) if idx1 is None else np.array(idx1)
		idx2 = np.arange(0, data.shape[1]) if idx2 is None else np.array(idx2)
        
		overlap = set(idx1) & set(idx2)
		
		overlap_idx1 = [i for i in idx1 if i in overlap]
		overlap_idx2 = [i for i in idx2 if i in overlap]
		
		overlap_idx1 = [new_i for new_i,i in enumerate(idx1) if i in overlap ]
		overlap_idx2 = [new_i for new_i,i in enumerate(idx2) if i in overlap ]
		
		row_weight = (1/size_factor).reshape([1, -1]) if size_factor is not None else np.ones(data.shape[0]).reshape([1, -1])
		X, Y = data[:, idx1].T.multiply(row_weight).T.tocsr(), data[:, idx2].T.multiply(row_weight).T.tocsr()
		prod = (X.T*Y).toarray()/X.shape[0]
		prod[overlap_idx1, overlap_idx2] = prod[overlap_idx1, overlap_idx2] - data[:, overlap_idx1].T.multiply(row_weight**2).T.tocsr().sum(axis=0).A1/n_obs
		cov = prod - np.outer(X.mean(axis=0).A1, Y.mean(axis=0).A1)
					
	return cov


def _hyper_1d_relative(data, n_obs, q, size_factor=None):
	"""
		Estimate the variance using the Poisson noise process.
		
		If :data: is a tuple, :cell_size: should be a tuple of (inv_sf, inv_sf_sq). Otherwise, it should be an array of length data.shape[0].
	"""
	if type(data) == tuple:
		size_factor = size_factor if size_factor is not None else (1, 1)
		mm_M1 = (data[0]*data[1]*size_factor[0]).sum(axis=0)/n_obs
		mm_M2 = (data[0]**2*data[1]*size_factor[1] - (1-q)*data[0]*data[1]*size_factor[1]).sum(axis=0)/n_obs
	else:
		
		row_weight = (1/size_factor).reshape([1, -1])
		row_weight_sq = (1/(size_factor**2 - size_factor*(1-q))).reshape([1, -1])
		mm_M1 = sparse.csc_matrix.dot(row_weight, data).ravel()/n_obs
		mm_M2 = sparse.csc_matrix.dot(row_weight_sq, data.power(2)).ravel()/n_obs - (1-q)*sparse.csc_matrix.dot(row_weight_sq, data).ravel()/n_obs
	
	mm_mean = mm_M1
	mm_var = (mm_M2 - mm_M1**2)

	return [mm_mean, mm_var]


def _hyper_cov_relative(data, n_obs, size_factor, q, idx1=None, idx2=None):
	"""
		Estimate the covariance using the hypergeometric noise process between genes at idx1 and idx2.
		
		If :data: is a tuple, :cell_size: should be a tuple of (inv_sf, inv_sf_sq). Otherwise, it should be an array of length data.shape[0].
	"""
	
	if type(data) == tuple:
		obs_M1 = (data[0]*data[2]*size_factor[0]).sum(axis=0)/n_obs
		obs_M2 = (data[1]*data[2]*size_factor[0]).sum(axis=0)/n_obs
		obs_MX = (data[0]*data[1]*data[2]*size_factor[1]).sum(axis=0)/n_obs
		cov = obs_MX - obs_M1*obs_M2

	else:

		idx1 = np.arange(0, data.shape[1]) if idx1 is None else np.array(idx1)
		idx2 = np.arange(0, data.shape[1]) if idx2 is None else np.array(idx2)
        
		overlap_location = (idx1 == idx2)
		overlap_idx = [i for i,j in zip(idx1, idx2) if i == j]
		
		row_weight = np.sqrt(1/(size_factor**2 + size_factor*(1-q))).reshape([1, -1])
		X, Y = data[:, idx1].T.multiply(row_weight).T.tocsr(), data[:, idx2].T.multiply(row_weight).T.tocsr()
		
		prod = X.multiply(Y).sum(axis=0).A1/n_obs
		prod[overlap_location] = prod[overlap_location] - (1-q)*data[:, overlap_idx].T.multiply(row_weight**2).T.tocsr().sum(axis=0).A1/n_obs
		cov = prod - X.mean(axis=0).A1*Y.mean(axis=0).A1
					
	return cov


def _corr_from_cov(cov, var_1, var_2, boot=False):
	"""
		Convert the estimation of the covariance to the estimation of correlation.
	"""
	
	if type(cov) != np.ndarray:
		return cov/np.sqrt(var_1*var_2)

		
	corr = np.full(cov.shape, 5.0)
	
	var_1[var_1 <= 0] = np.nan
	var_2[var_2 <= 0] = np.nan
	var_prod = np.sqrt(var_1*var_2)
		
	corr[np.isfinite(var_prod)] = cov[np.isfinite(var_prod)] / var_prod[np.isfinite(var_prod)]
	corr[(corr > 1) | (corr < -1)] = np.nan
	
	return corr
