"""
	hypothesis_test.py
	
	This file contains code to perform meta-analysis on the estimated parameters and their confidence intervals.
"""

import numpy as np
import scipy.stats as stats
from sklearn.linear_model import LinearRegression

import bootstrap
import estimator


def _push_nan(val):
		
	nan_idx = np.isnan(val)
	nan_count = nan_idx.sum()
	val[:(val.shape[0]-nan_count)] = val[~nan_idx]
	val[(val.shape[0]-nan_count):] = np.nan
	
	return val

def _compute_asl(perm_diff):
	""" 
		Use the generalized pareto distribution to model the tail of the permutation distribution. 
	"""
	
	extreme_count = (perm_diff > 0).sum()
	extreme_count = min(extreme_count, perm_diff.shape[0] - extreme_count)
	
# 	c = (perm_diff < 0).mean()
	
# 	return 2*min(1-c, c )
	
# 	stat = perm_diff[0]
# 	boot_stat =  perm_diff[1:]
# 	boot_stat = boot_stat[np.isfinite(boot_stat)]
	
# 	centered = boot_stat - stat

# 	c = ((perm_diff[1:] > 0).sum() + 1)/(perm_diff[1:].shape[0]+1)
	
# 	return 2*min(c, 1-c)
	
# 	return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))
	
	if extreme_count > 2: # We do not need to use the GDP approximation. 

		return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))

	else: # We use the GDP approximation

		try:

			perm_mean = perm_diff.mean()
			perm_dist = np.sort(perm_diff) if perm_mean < 0 else np.sort(-perm_diff) # For fitting the GDP later on
			perm_dist = perm_dist[np.isfinite(perm_dist)]
			N_exec = 300 # Starting value for number of exceendences

			while N_exec > 50:

				tail_data = perm_dist[-N_exec:]
				params = stats.genextreme.fit(tail_data)
				_, ks_pval = stats.kstest(tail_data, 'genextreme', args=params)

				if ks_pval > 0.05: # roughly a genpareto distribution
					return 2 * (N_exec/perm_diff.shape[0]) * stats.genextreme.sf(1, *params)
				else: # Failed to fit genpareto
					N_exec -= 30
			return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))

		except:

			# Failed to fit genpareto, return the upper bound
			return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))

def _ht_1d(
	true_mean, # list of means
	true_res_var, # list of residual variances
	cells, # list of sparse vectors/matrices
	approx_sf, # list of dense arrays
	design_matrix,
	Nc_list,
	num_boot,
	cov_idx,
	n_umi,
	mv_fit # list of tuples
	):
	
	good_idxs = np.zeros(design_matrix.shape[0], dtype=bool)
	
	# the bootstrap arrays
	boot_mean = np.zeros((design_matrix.shape[0], num_boot+1))*np.nan
	boot_var = np.zeros((design_matrix.shape[0], num_boot+1))*np.nan

	for group_idx in range(len(true_mean)):

		# Skip if any of the 1d moments are NaNs
		if np.isnan(true_mean[group_idx]) or np.isnan(true_res_var[group_idx]) or true_mean[group_idx] == 0:
			continue

		# This replicate is good
		good_idxs[group_idx] = True

		# Fill in the true value
		boot_mean[group_idx, 0], boot_var[group_idx, 0] = true_mean[group_idx], true_res_var[group_idx]		

		# Generate the bootstrap values
		mean, var = bootstrap._bootstrap_1d(
			data=cells[group_idx],
			size_factor=approx_sf[group_idx],
			num_boot=num_boot,
			n_umi=n_umi)
				
		res_var = estimator._residual_variance(mean, var, mv_fit[group_idx])
		
		# Minimize invalid values
		boot_mean[group_idx, 1:] = mean#_push_nan(mean)#[:num_boot]
		boot_var[group_idx, 1:] = res_var + 3#_push_nan(res_var)#[:num_boot]
	
	# Skip this gene
	if good_idxs.sum() == 0:
		return np.nan, np.nan, np.nan, np.nan
	
	# Log the values
	# FIX: come up with a better solution to use the full bootstrap distribution for residual variance
	boot_var[good_idxs,] = np.log(boot_var[good_idxs,])
	boot_mean[good_idxs,] = np.log(boot_mean[good_idxs,])

	vals = _regress_1d(
			design_matrix=design_matrix[good_idxs, :],
			boot_mean=boot_mean[good_idxs, :], 
			boot_var=boot_var[good_idxs, :],
			Nc_list=Nc_list[good_idxs],
			cov_idx=cov_idx)
	return vals


def _regress_1d(design_matrix, boot_mean, boot_var, Nc_list, cov_idx):
	"""
		Performs hypothesis testing for a single gene for many bootstrap iterations.
		
		Here, :X_center:, :X_center_Sq:, :boot_var:, :boot_mean: should have the same number of rows
	"""
	
	num_boot = boot_mean.shape[1]
	
	boot_mean = boot_mean[:, ~np.any(~np.isfinite(boot_mean), axis=0)]
	boot_var = boot_var[:, ~np.any(~np.isfinite(boot_var), axis=0)]
	
	if boot_var.shape[1] < num_boot*0.5:
		return np.nan, np.nan, np.nan, np.nan
	
	mean_coef = LinearRegression(fit_intercept=False, n_jobs=1)\
		.fit(design_matrix, boot_mean, Nc_list).coef_[:, cov_idx]
	var_coef = LinearRegression(fit_intercept=False, n_jobs=1)\
		.fit(design_matrix, boot_var, Nc_list).coef_[:, cov_idx]

	mean_asl = _compute_asl(mean_coef[1:])
	var_asl = _compute_asl(var_coef[1:])
	
	return mean_coef[0], mean_asl, var_coef[0], var_asl


def _ht_2d(
	true_corr, # list of correlations for each group
	cells, # list of Nx2 sparse matrices
	approx_sf,
	n_umi,
	design_matrix,
	Nc_list,
	num_boot,
	cov_idx):
		
	good_idxs = np.zeros(design_matrix.shape[0], dtype=bool)
	
	# the bootstrap arrays
	boot_corr = np.zeros((design_matrix.shape[0], num_boot+1))*np.nan

	for group_idx in range(design_matrix.shape[0]):

		# Skip if any of the 2d moments are NaNs
		if np.isnan(true_corr[group_idx]) or (np.abs(true_corr[group_idx]) == 1):
			continue

		# Fill in the true value
		boot_corr[group_idx, 0] = true_corr[group_idx]
		
		# Generate the bootstrap values
		cov, var_1, var_2 = bootstrap._bootstrap_2d(
			data=cells[group_idx],
			size_factor=approx_sf[group_idx],
			num_boot=int(num_boot),
			n_umi=n_umi)
		
# 		var_1[var_1 < 0] = np.nanmean(var_1)
# 		var_2[var_2 < 0] = np.nanmean(var_2)
				
		corr = estimator._corr_from_cov(cov, var_1, var_2, boot=True)

		# Skip if too many bootstrap iterations give NaNs
		nan_count = (~np.isfinite(corr)).sum()
		if nan_count > 0.2*num_boot:
			continue
			
		# This replicate is good
		good_idxs[group_idx] = True
		boot_corr[group_idx, 1:] = corr#_push_nan(corr)#[:num_boot]

	# Skip this gene
	if good_idxs.sum() == 0:
		return np.nan, np.nan
	
	# Skip if each covariate group is not represented
	if np.unique(design_matrix[good_idxs, cov_idx]).shape[0] == 1:
		return np.nan, np.nan
	
	vals = _regress_2d(
			design_matrix=design_matrix[good_idxs, :],
			boot_corr=boot_corr[good_idxs, :],
			Nc_list=Nc_list[good_idxs],
			cov_idx=cov_idx)
	
	return vals


def _regress_2d(design_matrix, boot_corr, Nc_list, cov_idx):
	"""
		Performs hypothesis testing for a single pair of genes for many bootstrap iterations.
	"""
	
	num_boot = boot_corr.shape[1]
	
	boot_corr = boot_corr[:, ~np.any(~np.isfinite(boot_corr), axis=0)]
	
	corr_coef = LinearRegression(fit_intercept=False, n_jobs=4)\
		.fit(design_matrix, boot_corr, Nc_list).coef_[:, cov_idx]
	
	if boot_corr.shape[1] < num_boot*0.7:
		return corr_coef[0], np.nan

	corr_asl = _compute_asl(corr_coef[1:])
	
	return corr_coef[0], corr_asl
