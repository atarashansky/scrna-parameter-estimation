"""
	scmemo.py

	Single Cell Moment Estimator

	This file contains code for implementing the empirical bayes estimator for the Gaussian assumption for true single cell RNA sequencing counts.
"""


import pandas as pd
import scipy.stats as stats
import scipy.sparse as sparse
import numpy as np
import time
import itertools
import scipy as sp
import logging
from scipy.stats import multivariate_normal
import pickle as pkl
import sklearn.decomposition as decomp
import matplotlib
matplotlib.use('AGG')
import matplotlib.pyplot as plt
import sklearn as sk

from utils import *


def _compute_1d_statistics(observed, N, smooth=True):
	""" Compute some non central moments of the observed data. """

	pseudocount = 1/observed.shape[1] if smooth else 0

	if type(observed) != np.ndarray:

		# count unique values in each column
		#unique_counts = pd.DataFrame(observed.toarray()).nunique().values

		first = (observed.sum(axis=0).A1 + pseudocount)/(observed.shape[0]+pseudocount*observed.shape[1])
		c = observed.copy()
		c.data **= 2
		second = (c.sum(axis=0).A1 + pseudocount)/(observed.shape[0]+pseudocount*observed.shape[1])
		del c

	else:

		#unique_counts = pd.DataFrame(observed).nunique().values
		first = (observed.sum(axis=0) + pseudocount)/(observed.shape[0]+pseudocount*observed.shape[1])
		second = ((observed**2).sum(axis=0) + pseudocount)/(observed.shape[0]+pseudocount*observed.shape[1])

	# Return the first moment, second moment, expectation of the product
	# Make some columns into NaN
	#econd[unique_counts <= 2] = np.nan
	return first, second


def _estimate_mean(observed_first, q):
	""" Estimate the mean vector. """

	return observed_first/q


def _estimate_variance(observed_first, observed_second, mean_inv_allgenes, q, q_sq):
	""" Estimate the true variance. """

	numer = observed_first - (q_sq/q)*observed_first - observed_second
	denom = -q_sq + q*mean_inv_allgenes - q_sq*mean_inv_allgenes

	return numer/denom - observed_first**2/q**2


def _estimate_covariance(observed_first_1, observed_first_2, observed_prod, mean_inv_allgenes, q, q_sq):
	""" Estimate the true covariance. """

	# Estimate covariances except for the diagonal


	denom = q_sq - (q - q_sq)*mean_inv_allgenes
	cov = observed_prod / denom - observed_first_1.reshape(-1,1)@observed_first_2.reshape(1, -1)/q**2

	return cov


def _compute_mean_inv_numis(observed_allgenes_mean, observed_allgenes_variance, q, q_sq):
    """
        Compute the expected value of inverse of N-1.
    """

    denom = observed_allgenes_mean**3 / q**3

    numer = \
        observed_allgenes_variance/q_sq + \
        observed_allgenes_mean/q_sq + \
        observed_allgenes_mean/q + \
        observed_allgenes_mean**2/q**2

    return numer/denom


def _mean_substitution(mat):
	""" Perform mean substition. Get the percentage of missing values. This will lower the power, but should still be unbiased. """

	to_return = mat.copy()
	col_mean = np.nanmean(mat, axis=0)
	col_mean[np.isnan(col_mean)] = 0
	isnan_mat = np.isnan(mat)
	inds = np.where(isnan_mat)
	perc_nans = np.isnan(mat).sum(axis=0)/mat.shape[0]
	to_return[inds] = np.take(col_mean, inds[1])

	return perc_nans, to_return


class SingleCellEstimator(object):
	"""
		SingleCellEstimator is the class for fitting univariate and bivariate single cell data. 

		:q: Expected value of the capture rate.
	"""


	def __init__(
		self, 
		adata,
		n_umis_column,
		covariate_label=None,
		replicate_label=None,
		batch_label=None,
		subsection_label=None,
		num_permute=10000,
		label_delimiter='^',
		covariate_converter={},
		q=0.1,
		smooth=True,
		use_hat_matrix=False):

		# Copy over the anndata object
		self.anndata = adata.copy()
		self.is_dense = type(adata.X) != np.ndarray

		# Keep q and labels
		self.q = q
		self.group_label = 'scmemo_group'
		self.covariate_label = covariate_label if covariate_label else 'default_cov'
		self.replicate_label = replicate_label if replicate_label else 'default_rep'
		self.batch_label = batch_label if batch_label else 'default_batch'
		self.subsection_label = subsection_label
		self.label_delimiter = label_delimiter

		# Form discrete groups
		if covariate_label is None:
			self.anndata.obs[self.covariate_label] = 'default_cov'
		if replicate_label is None:
			self.anndata.obs[self.replicate_label] = 'default_rep'
		if batch_label is None:
			self.anndata.obs[self.batch_label] = 'default_batch'
		if subsection_label is None:
			self.anndata.obs[self.subsection_label] = 'default_subsection'

		self.anndata.obs[self.group_label] = 'sg' + self.label_delimiter + \
			self.anndata.obs[self.covariate_label].astype(str) + self.label_delimiter + \
			self.anndata.obs[self.replicate_label].astype(str) + self.label_delimiter + \
			self.anndata.obs[self.batch_label].astype(str) + self.label_delimiter + \
			self.anndata.obs[self.subsection_label].astype(str)
		self.groups = self.anndata.obs[self.group_label].drop_duplicates().tolist() + ['all']
		
		# Keep n_umis, num_permute, cov converter
		self.n_umis = adata.obs[n_umis_column].values
		self.num_permute = num_permute
		self.covariate_converter = covariate_converter
		self.smooth = smooth
		
		# Initialize mean-var relationship params
		self.mean_var_slope = None
		self.mean_var_inter = None

		# Initialize parameter containing dictionaries
		self.mean_inv_numis = {}
		self.observed_moments = {}
		self.observed_central_moments = {}
		self.estimated_central_moments = {}
		self.parameters = {}
		self.parameters_confidence_intervals = {}

		# Attributes for hypothesis testing
		self.use_hat_matrix = use_hat_matrix
		self.hypothesis_test_result = {}
		self.hypothesis_test_result_2d = {}

		# Cache for selecting cells
		self.group_cells = {}


	def _get_covariate(self, group):
		return group.split(self.label_delimiter)[1]


	def _get_replicate(self, group):
		return group.split(self.label_delimiter)[2]


	def _get_batch(self, group):
		return group.split(self.label_delimiter)[3]


	def _get_subsection(self, group):
		return group.split(self.label_delimiter)[4]


	def _select_cells(self, group, n_umis=False):
		""" Select the cells. """

		if group in self.group_cells and not n_umis:
			return self.group_cells[group]

		if group == 'all': # All cells
			cell_selector = np.arange(self.anndata.shape[0])
		elif group[0] == '-': # Exclude this group
			cell_selector = (self.anndata.obs[self.group_label] != group[1:]).values
		else: # Include this group
			cell_selector = (self.anndata.obs[self.group_label] == group).values

		if n_umis: # select the n_umis
			return self.n_umis[cell_selector]

		data = self.anndata.X[cell_selector, :] if group == 'all' or self.is_dense else self.anndata.X[cell_selector, :].toarray()

		self.group_cells[group] = data.copy()
		return self.group_cells[group]


	def _get_gene_idxs(self, gene_list):
		""" Returns the indices of each gene in the list. """

		return np.array([np.where(self.anndata.var.index == gene)[0][0] for gene in gene_list]) # maybe use np.isin


	def _estimate_residual_variance(self, estimated_mean, estimated_var):
		""" 
			Estimate the residual variance by performing linear regression. 

			Returns the residual variance as well as its logarithm.
		"""

		log_residual_variance = np.log(estimated_var) - (self.mean_var_slope*np.log(estimated_mean) + self.mean_var_inter)

		return np.exp(log_residual_variance), log_residual_variance


	def _compute_params(self, group='all'):
		""" 
			Use the estimated moments to compute the parameters of marginal distributions as 
			well as the estimated correlation. 
		"""

		residual_variance, log_residual_variance = self._estimate_residual_variance(
			self.estimated_central_moments[group]['first'],
			self.estimated_central_moments[group]['second'])

		self.parameters[group] = {
			'mean': self.estimated_central_moments[group]['first'],
			'log_mean': np.log(self.estimated_central_moments[group]['first']),
			'residual_var':residual_variance,
			'log_residual_var':log_residual_variance,
			'cov':sparse.lil_matrix(
				(self.anndata.shape[1], self.anndata.shape[1]), dtype=np.float32),
			'corr': sparse.lil_matrix(
				(self.anndata.shape[1], self.anndata.shape[1]), dtype=np.float32)}


	def _compute_estimated_1d_moments(self, group='all'):
		""" Use the observed moments to compute the moments of the underlying distribution. """

		mean_inv_numis = _compute_mean_inv_numis(
			self.observed_central_moments[group]['allgenes_first'], 
			self.observed_central_moments[group]['allgenes_second'],
			self.q,
			self.q_sq)

		estimated_mean = _estimate_mean(self.observed_moments[group]['first'], self.q)

		estimated_var = _estimate_variance(
			self.observed_moments[group]['first'], 
			self.observed_moments[group]['second'],
			mean_inv_numis,
			self.q,
			self.q_sq)
		estimated_var[estimated_var < 0] = np.nan

		self.estimated_central_moments[group] = {
			'first': estimated_mean,
			'second': estimated_var,
			'prod': sparse.lil_matrix(
				(self.anndata.shape[1], self.anndata.shape[1]), dtype=np.float32)
		}


	def _get_effect_size(self, response_variable, test_dict, nan_thresh=0.5):
		""" Perform the regression for a particular subsection. """

		percent_missing, response_variable = _mean_substitution(response_variable)

		if self.use_hat_matrix:
			effect_sizes = test_dict['hat_matrix'].dot(response_variable)[0, :]
		else:
			effect_sizes = \
				sk.linear_model.LinearRegression(fit_intercept=False)\
				.fit(test_dict['design_matrix'], response_variable, test_dict['cell_count']).coef_[:, 0]

		effect_sizes[percent_missing > nan_thresh] = np.nan

		return effect_sizes


	def compute_observed_moments(self, verbose=False):
		""" Compute the observed statistics. Does not compute the covariance. """

		for group in self.groups:

			if verbose:
				print('Computing observed moments for:', group)

			observed = self._select_cells(group)
			N = observed.shape[0]

			first, second = _compute_1d_statistics(observed, N, smooth=self.smooth)
			allgenes_first = self._select_cells(group, n_umis=True).mean()
			allgenes_second = (self._select_cells(group, n_umis=True)**2).mean()

			self.observed_moments[group] = {
				'first':first,
				'second':second,
				'prod':sparse.lil_matrix(
					(self.anndata.shape[1], self.anndata.shape[1]), dtype=np.float32),
				'allgenes_first':allgenes_first,
				'allgenes_second':allgenes_second,
			}

			self.observed_central_moments[group] = {
				'first':first,
				'second':second-first**2,
				'prod':sparse.lil_matrix(
					(self.anndata.shape[1], self.anndata.shape[1]), dtype=np.float32),
				'allgenes_first':allgenes_first,
				'allgenes_second':allgenes_second - allgenes_first**2
			}


	def estimate_q_sq(self, frac=0.3, verbose=True):
		""" 
			Estimate the expected value of the square of the capture rate q. 

			Estimate the relationship between mean and variance. This means that we assume the mean variance relationship to be 
			the same for all cell types. 
		"""

		# Combine all observed moments from every group
		x = self.observed_central_moments['all']['first']
		y = self.observed_central_moments['all']['second']

		# Filter for finite estimates
		condition = np.isfinite(x) & np.isfinite(y)
		x = x[condition]
		y = y[condition]

		# Filter for top k genes
		k = int(frac*self.anndata.shape[1])
		k_largest_idx = np.argpartition(x, -k)[-k:]
		self.k_largest_indices = k_largest_idx
		x = x[k_largest_idx]
		y = y[k_largest_idx]

		# Get the upper and lower limits for q_sq
		lower_lim = self.q**2
		upper_lim = (self.n_umis**2).mean()/self.n_umis.mean()**2*self.q**2


		# Estimate the noise level, or CV^2_q + 1
		noise_level = np.nanmin((y/x**2 - 1/x)[y > x])

		# Estimate the initial guess for q_sq
		initial_q_sq_estimate = (noise_level+1)*self.q**2

		# Refine the initial guess
		res = sp.optimize.minimize_scalar(
			estimated_mean_disp_corr, 
			bounds=[lower_lim, upper_lim], 
			args=(self, frac),
			method='bounded',
			options={'maxiter':100, 'disp':verbose})
		q_sq_estimate = res.x

		# Clear out estimated parameters
		self.parameters = {}

		# Keep estimated parameters for later
		self.noise_level = q_sq_estimate/self.q**2-1
		self.q_sq = q_sq_estimate

		if verbose:
			print('E[q^2] falls in [{:.5f}, {:.8f}], with the current estimate of {:.8f}'.format(lower_lim, upper_lim, self.q_sq))


	def plot_cv_mean_curve(self, group='all', estimated=False, plot_noise=True):
		"""
			Plot the observed characteristic relationship between the mean and the coefficient of variation. 

			If an estimate for q_sq exists, also plot the estimated baseline noise level.
		"""

		obs_mean = self.estimated_central_moments[group]['first'] if estimated else self.observed_central_moments[group]['first']
		obs_var = self.estimated_central_moments[group]['second'] if estimated else self.observed_central_moments[group]['second']

		# Filter NaNs
		condition = np.isfinite(obs_mean) & np.isfinite(obs_var)
		obs_mean = obs_mean[condition]
		obs_var = obs_var[condition]


		plt.scatter(
		    np.log(obs_mean),
		    np.log(obs_var)/2-np.log(obs_mean),
		    s=2,
		    alpha=0.5
		)

		# plt.scatter(
		#     np.log(obs_mean)[self.k_largest_indices],
		#     (np.log(obs_var)/2-np.log(obs_mean))[self.k_largest_indices],
		#     s=2,
		#     alpha=0.5
		# )

		bound_x = np.arange(
		    np.nanmin(obs_mean),
		    np.nanmax(obs_mean),
		    0.01)
		bound_y = 1/bound_x + self.q_sq/self.q**2-1

		plt.plot(np.log(bound_x), -np.log(bound_x)/2, color='k', lw=2)

		if not estimated and plot_noise:
			plt.plot(np.log(bound_x), np.log(bound_y)/2, lw=2, color='r')
		plt.axis('equal');
		plt.legend(['Poisson', 'Poisson + noise', 'genes'])
		plt.title('Observed Mean - CV Relationship');
		plt.xlabel('log( observed mean )')
		plt.ylabel('log( observed CV )')


	def estimate_1d_parameters(self):
		""" Perform 1D (mean, variability) parameter estimation. """

		# Compute estimated moments
		for group in self.groups:
			self._compute_estimated_1d_moments(group)

		# Combine all estimated moments from every group
		x = np.concatenate([self.estimated_central_moments[group]['first'] for group in self.groups])
		y = np.concatenate([self.estimated_central_moments[group]['second'] for group in self.groups])

		# Filter for finite estimates
		condition = np.isfinite(x) & np.isfinite(y)
		x = x[condition]
		y = y[condition]

		# Estimate the mean-var relationship
		if not self.mean_var_slope and not self.mean_var_inter:
			slope, inter, _, _, _ = robust_linregress(np.log(x), np.log(y))
			self.mean_var_slope = slope
			self.mean_var_inter = inter

		# Compute final parameters
		for group in self.groups:
			self._compute_params(group)


	def estimate_2d_parameters(self, gene_list_1, gene_list_2, groups='all'):
		""" Perform 2D parameter estimation. """

		groups_to_iter = groups if groups != 'all' else self.groups
		gene_idxs_1 = self._get_gene_idxs(gene_list_1)
		gene_idxs_2 = self._get_gene_idxs(gene_list_2)

		for group in groups_to_iter:

			mean_inv_numis = _compute_mean_inv_numis(
				self.observed_central_moments[group]['allgenes_first'], 
				self.observed_central_moments[group]['allgenes_second'],
				self.q,
				self.q_sq)

			observed = self._select_cells(group)
			observed_1 = observed[:, gene_idxs_1].copy()
			observed_2 = observed[:, gene_idxs_2].copy()

			# Compute the observed cross-covariance and expectation of the product
			observed_cov, observed_prod = cross_covariance(observed_1, observed_2)

			# Update the observed dictionaries
			self.observed_moments[group]['prod'][gene_idxs_1[:, np.newaxis], gene_idxs_2] = observed_prod
			self.observed_central_moments[group]['prod'][gene_idxs_1[:, np.newaxis], gene_idxs_2] = observed_cov

			# Estimate the true covariance
			estimated_cov = _estimate_covariance(
				self.observed_central_moments[group]['first'][gene_idxs_1],
				self.observed_central_moments[group]['first'][gene_idxs_2],
				observed_prod,
				mean_inv_numis,
				self.q,
				self.q_sq)

			# Estimate the true correlation
			vars_1 = self.estimated_central_moments[group]['second'][gene_idxs_1]
			vars_2 = self.estimated_central_moments[group]['second'][gene_idxs_2]
			estimated_corr = estimated_cov / np.sqrt(vars_1[:, np.newaxis]).dot(np.sqrt(vars_2[np.newaxis, :]))
			estimated_corr[estimated_corr > 1] = 1
			estimated_corr[estimated_corr < -1] = -1

			# Update the estimated dictionaries
			self.estimated_central_moments[group]['prod'][gene_idxs_1[:, np.newaxis], gene_idxs_2] = estimated_cov
			self.parameters[group]['cov'][gene_idxs_1[:, np.newaxis], gene_idxs_2] = estimated_cov
			self.parameters[group]['corr'][gene_idxs_1[:, np.newaxis], gene_idxs_2] = estimated_corr


	def setup_hypothesis_testing(self, subsections=[]):
		"""
			Perform operations necessarily to set up hypothesis testing for both 1D and 2D. 

			Construct the design matrix. If applicable, compute the hat matrix for each subsection.
		"""

		for subsection in subsections:

			# Setup the hypothesis result dict
			self.hypothesis_test_result[subsection] = {}

			# List of meta observations to construct DF later
			covariate_list = []

			# Get all the appropriate groups
			subsection_groups = [group for group in self.groups if group != 'all' and self._get_subsection(group) == subsection]
			self.hypothesis_test_result[subsection]['groups'] = subsection_groups

			# Iterate through each group
			for group in subsection_groups:

				# Ignore the groups in other subsections
				if self._get_subsection(group) != subsection:
					continue

				covariate, replicate, batch = self._get_covariate(group), self._get_replicate(group), self._get_batch(group)
				covariate_list.append((
					self.covariate_converter[covariate], # Numerical covariate
					replicate, # Replicate (e.g., individual)
					batch, # Batch
					1, # Constant for fitting the intercept
					self.group_cells[group].shape[0], # Number of cells in this group for weighting
					))
			
			subsection_design_df = pd.DataFrame(covariate_list, columns=['covariate', 'replicate', 'batch', 'constant', 'cell_count'])
			self.hypothesis_test_result[subsection]['design_df'] = subsection_design_df.copy()

			# Save the cell counts
			cell_count = subsection_design_df['cell_count'].values
			self.hypothesis_test_result[subsection]['cell_count'] = cell_count

			# Construct the design matrix
			if subsection_design_df['batch'].nunique() < 2:
				design_matrix = subsection_design_df[['covariate', 'constant']].values
			else:
				design_matrix = pd.get_dummies(subsection_design_df[['covariate', 'batch', 'constant']], columns=['batch'], drop_first=True).values
			self.hypothesis_test_result[subsection]['design_matrix'] = design_matrix

			# Compute the hat matrix
			if self.use_hat_matrix:
				hat_matrix = np.linalg.inv(design_matrix.T.dot(cell_count.reshape(-1,1)*design_matrix)).dot(design_matrix.T*cell_count)
				self.hypothesis_test_result[subsection]['hat_matrix'] = hat_matrix


	def compute_effect_sizes_1d(self):
		""" 
			Compute the effect sizes for mean and variability. 
			This function does not compute p-values or FDR.
			Assumes that the 1D parameters have been estimated.
			Assumes that the setup_hypothesis_testing function has been run already.
		"""

		for subsection, test_dict in self.hypothesis_test_result.items():

			log_means = np.vstack([self.parameters[group]['log_mean'] for group in test_dict['groups']])
			log_residual_vars = np.vstack([self.parameters[group]['log_residual_var'] for group in test_dict['groups']])

			mean_es = self._get_effect_size(log_means, test_dict)
			var_es = self._get_effect_size(log_residual_vars, test_dict)

			test_dict['de_effect_size'], test_dict['dv_effect_size'] = mean_es, var_es


	def compute_effect_sizes_2d(self, gene_list_1, gene_list_2):
		"""
			Compute the effect sizes for correlations.
			This function does not compute p-values or FDR.
			Assumes that the 2D parameters for the same gene_lists have been estimated. 
			Assumes that the setup_hypothesis_testing function has been run already.
		"""

		# for subsection, test_dict in self.hypothesis_test_result.items():

		# 	correlations = 


	def compute_confidence_intervals_1d(self, hypothesis_test=True, gene_tracker_count=100, verbose=False, timer='off'):
		"""
			Compute confidence intervals and p-values for estimate and effect sizes. 

			Use the multinomial resampling.

			Uses self.num_permute attribute, same as in hypothesis testing.

			CAVEAT: Uses the same expectation of 1/N as the true value, does not compute this from the permutations. 
			So the result might be slightly off.
		"""

		# Get the starting time
		start_time = time.time()

		# Calculate the pseudocount, so that the demonimator is N+1
		pseudocount = 1/self.anndata.shape[1] if self.smooth else 0
		
		# Get the relevant groups to iterate over. 
		if hypothesis_test:
			groups_to_iter = []
			for subsection, test_dict in self.hypothesis_test_result.items():
				groups_to_iter += test_dict['groups']
		else: 
			groups_to_iter = self.groups

		# Compute the inverse of N
		mean_inv_numis = {
			group:_compute_mean_inv_numis(
				self.observed_central_moments[group]['allgenes_first'], 
				self.observed_central_moments[group]['allgenes_second'],
				self.q,
				self.q_sq) for group in groups_to_iter}

		# Declare placeholders for gene confidence intervals
		parameters = ['mean', 'residual_var', 'log_mean', 'log_residual_var', 'log1p_mean', 'log1p_residual_var']
		ci_dict = {
			group:{param:np.zeros(self.anndata.var.shape[0]) for param in parameters} \
				for group in groups_to_iter}

		# Declare placeholders for hypothesis testing result
		parameters = ['de_pval', 'de_fdr', 'de_es_ci', 'dv_pval', 'dv_fdr', 'dv_es_ci']
		for subsection, test_dict in self.hypothesis_test_result.items():
			for parameter in parameters:
				test_dict[parameter] = np.full(self.anndata.var.shape[0], np.nan)

		# Iterate through each gene and compute a standard error for each gene
		for gene_idx in range(self.anndata.var.shape[0]):
			
			if verbose and gene_tracker_count > 0 and gene_idx % gene_tracker_count == 0: 
				print('Computing the {}st/th gene, {:.5f} seconds have passed.'.format(gene_idx, time.time()-start_time))

			gene_mult_rvs = {}
			gene_counts = {}
			gene_freqs = {}
			for group in groups_to_iter:

				# Grab the values
				data = self._select_cells(group)
				if type(data) != np.ndarray:
					data = data.toarray()
				
				count_start_time = time.time()
				counts = np.bincount(data[:, gene_idx].reshape(-1).astype(int))
				count_time = time.time() - count_start_time
				
				expr_values = np.arange(counts.shape[0])
				expr_values = expr_values[counts != 0]
				counts = counts[counts != 0]
				gene_counts[group] = expr_values
				gene_freqs[group] = counts.copy()/data.shape[0]
			
			compute_start_time = time.time()
			
			for group in groups_to_iter:
				gene_mult_rvs[group] = stats.multinomial.rvs(n=data.shape[0], p=gene_freqs[group], size=self.num_permute)

			# Construct the repeated values matrix
			values = {group:np.tile(
				gene_counts[group].reshape(1, -1), (self.num_permute, 1)) for group in groups_to_iter}

			# Compute the permuted, observed mean/dispersion
			mean = {group:((gene_mult_rvs[group] * values[group]).sum(axis=1)+pseudocount)/(data.shape[0]+1) for group in groups_to_iter}
			second_moments = {group:((gene_mult_rvs[group] * values[group]**2).sum(axis=1)+pseudocount)/(data.shape[0]+1) for group in groups_to_iter}
			del gene_mult_rvs
			del gene_counts

			# Compute the permuted, estimated moments for both groups
			estimated_means = {group:_estimate_mean(mean[group], self.q) for group in groups_to_iter}
			estimated_vars = {group:_estimate_variance(mean[group], second_moments[group], mean_inv_numis[group], self.q, self.q_sq) for group in groups_to_iter}
			estimated_residual_vars = {group:self._estimate_residual_variance(estimated_means[group], estimated_vars[group])[0] for group in groups_to_iter}

			compute_time = time.time()-compute_start_time
			
			# Store the S.E. of the parameter, log(param), and log1p(param)		
			for group in groups_to_iter:

				ci_dict[group]['mean'][gene_idx] = np.nanstd(estimated_means[group])
				ci_dict[group]['residual_var'][gene_idx] = np.nanstd(estimated_residual_vars[group])
				ci_dict[group]['log_mean'][gene_idx] = np.nanstd(np.log(estimated_means[group]))
				ci_dict[group]['log_residual_var'][gene_idx] = np.nanstd(np.log(estimated_residual_vars[group]))
				ci_dict[group]['log1p_mean'][gene_idx] = np.nanstd(np.log(estimated_means[group]+1))
				ci_dict[group]['log1p_residual_var'][gene_idx] = np.nanstd(np.log(estimated_residual_vars[group]+1))
			
			# Perform hypothesis testing
			for subsection, test_dict in self.hypothesis_test_result.items():

				# Organize the data into format for meta-analysis
				boot_log_means = np.vstack([np.log(estimated_means[group]) for group in test_dict['groups']])
				boot_log_residual_vars = np.vstack([np.log(estimated_residual_vars[group]) for group in test_dict['groups']])

				# Compute the effect sizes
				mean_es = self._get_effect_size(boot_log_means, test_dict)
				var_es = self._get_effect_size(boot_log_residual_vars, test_dict)

				# Update the test dict
				if np.isfinite(self.hypothesis_test_result[subsection]['de_effect_size'][gene_idx]):
					test_dict['de_es_ci'][gene_idx] = np.nanstd(mean_es)
					test_dict['de_pval'][gene_idx] = compute_asl(mean_es)
				if np.isfinite(self.hypothesis_test_result[subsection]['dv_effect_size'][gene_idx]):
					test_dict['dv_es_ci'][gene_idx] = np.nanstd(var_es)
					test_dict['dv_pval'][gene_idx] = compute_asl(var_es)

		# Perform FDR correction
		for subsection, test_dict in self.hypothesis_test_result.items():
			test_dict['de_fdr'] = fdrcorrect(test_dict['de_pval'])
			test_dict['dv_fdr'] = fdrcorrect(test_dict['dv_pval'])

		# Update the attribute dictionaries
		self.parameters_confidence_intervals.update(ci_dict)

		if timer == 'on':
			return count_time, compute_time, sum([values[group].shape[1] for group in groups_to_iter])/len(groups_to_iter)
		

	def compute_confidence_intervals_2d(self, gene_list_1, gene_list_2, groups=None, groups_to_compare=None, gene_tracker_count=100, verbose=False):
		"""
			Compute 95% confidence intervals around the estimated parameters. 

			Use the Gamma -> Dirichlet framework to speed up the process.

			Uses self.num_permute attribute, same as in hypothesis testing.

			CAVEAT: Uses the same expectation of 1/N as the true value, does not compute this from the permutations. 
			So the result might be slightly off.
		"""

		groups_to_iter = self.groups if groups == 'all' else groups
		comparison_groups = groups_to_compare if groups_to_compare else list(itertools.combinations(groups_to_iter, 2))

		mean_inv_numis = {
			group:_compute_mean_inv_numis(
				self.observed_central_moments[group]['allgenes_first'], 
				self.observed_central_moments[group]['allgenes_second'],
				self.q,
				self.q_sq) for group in groups_to_iter}

		# Get the gene idx for the two gene lists
		genes_idxs_1 = self._get_gene_idxs(gene_list_1)
		genes_idxs_2 = self._get_gene_idxs(gene_list_2)

		# all_pair_counts = {group:set() for group in groups_to_iter}
		pair_counts = {}

		# Declare placeholders for gene confidence intervals
		parameters = ['cov', 'corr']
		ci_dict = {
			group:{param:np.zeros(self.parameters[groups_to_iter[0]]['corr'].shape)*np.nan for param in parameters} \
				for group in groups_to_iter}

		# Declare placeholders for group comparison results
		hypothesis_test_parameters = [
			'dcov_diff', 'dcov_pval', 'dcov_fdr', 'cov_1', 'cov_2',
			'dcorr_diff', 'dcorr_pval', 'dcorr_fdr', 'corr_1', 'corr_2'
			]
		hypothesis_test_dict = {
			(group_1, group_2):{param:np.zeros(self.parameters[group_1]['corr'].shape)*np.nan for param \
				in hypothesis_test_parameters} for group_1, group_2 in comparison_groups}
		for group_1, group_2 in comparison_groups:
			hypothesis_test_dict[(group_1, group_2)]['gene_idx_1'] = genes_idxs_1
			hypothesis_test_dict[(group_1, group_2)]['gene_idx_2'] = genes_idxs_2

		# Iterate through each gene and compute a standard error for each gene
		iter_1 = 0
		iter_2 = 0
		for gene_idx_1 in genes_idxs_1:

			iter_2 = 0
			for gene_idx_2 in genes_idxs_2:

				if gene_idx_2 == gene_idx_1:
					continue

				if verbose and gene_tracker_count > 0 and (iter_1*genes_idxs_2.shape[0] + iter_2) % gene_tracker_count == 0: 
					print('Computing the {}st/th gene of {}'.format((iter_1*genes_idxs_2.shape[0] + iter_2), genes_idxs_1.shape[0]*genes_idxs_2.shape[0]))

				gene_dir_rvs = {}
				gene_counts = {}
				for group in groups_to_iter:

					# Grab the values
					data = self._select_cells(group)
					if type(data) != np.ndarray:
						data = data.toarray()
					cantor_code = pair(data[:, gene_idx_1], data[:, gene_idx_2])

					expr_values, counts = np.unique(cantor_code, return_counts=True)

					pair_counts[group] = expr_values
					gene_dir_rvs[group] = stats.dirichlet.rvs(alpha=counts, size=self.num_permute)

				# Construct the repeated values matrix
				cantor_code = {group:pair_counts[group] for group in groups_to_iter}
				values_1 = {}
				values_2 = {}

				for group in groups_to_iter:
					values_1_raw, values_2_raw = depair(cantor_code[group])
					values_1[group] = np.tile(values_1_raw.reshape(1, -1), (self.num_permute, 1))
					values_2[group] = np.tile(values_2_raw.reshape(1, -1), (self.num_permute, 1))

				# Compute the bootstrapped observed moments
				mean_1 = {group:((gene_dir_rvs[group]) * values_1[group]).sum(axis=1) for group in groups_to_iter}
				second_moments_1 = {group:((gene_dir_rvs[group]) * values_1[group]**2).sum(axis=1) for group in groups_to_iter}
				mean_2 = {group:((gene_dir_rvs[group]) * values_2[group]).sum(axis=1) for group in groups_to_iter}
				second_moments_2 = {group:((gene_dir_rvs[group]) * values_2[group]**2).sum(axis=1) for group in groups_to_iter}
				prod = {group:((gene_dir_rvs[group]) * values_1[group] * values_2[group]).sum(axis=1) for group in groups_to_iter}
				del gene_dir_rvs

				# Compute the permuted, estimated moments for both groups
				estimated_means_1 = {group:_estimate_mean(mean_1[group], self.q) for group in groups_to_iter}
				estimated_vars_1 = {group:_estimate_variance(mean_1[group], second_moments_1[group], mean_inv_numis[group], self.q, self.q_sq) for group in groups_to_iter}
				estimated_means_2 = {group:_estimate_mean(mean_2[group], self.q) for group in groups_to_iter}
				estimated_vars_2 = {group:_estimate_variance(mean_2[group], second_moments_2[group], mean_inv_numis[group], self.q, self.q_sq) for group in groups_to_iter}

				# Compute estimated correlations
				estimated_corrs = {}
				estimated_covs = {}
				for group in groups_to_iter:
					denom = self.q_sq - (self.q - self.q_sq)*mean_inv_numis[group]
					cov = prod[group] / denom - (mean_1[group] * mean_2[group])/self.q**2
					estimated_covs[group] = cov
					estimated_corrs[group] = cov / np.sqrt(estimated_vars_1[group]*estimated_vars_2[group]) 

				# Store the S.E. of the correlation
				for group in groups_to_iter:
					ci_dict[group]['cov'][gene_idx_1, gene_idx_2] = np.nanstd(estimated_covs[group])
					ci_dict[group]['corr'][gene_idx_1, gene_idx_2] = np.nanstd(estimated_corrs[group])

				# Perform hypothesis testing
				for group_1, group_2 in comparison_groups:

					# For difference of covariances
					boot_cov_diff = estimated_covs[group_2] - estimated_covs[group_1]
					observed_cov_diff = self.parameters[group_2]['cov'][gene_idx_1, gene_idx_2] - self.parameters[group_1]['cov'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['cov_1'][gene_idx_1, gene_idx_2]  = self.parameters[group_1]['cov'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['cov_2'][gene_idx_1, gene_idx_2]  = self.parameters[group_2]['cov'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['dcov_diff'][gene_idx_1, gene_idx_2]  = observed_cov_diff

					# For difference of correlations
					boot_corr_diff = estimated_corrs[group_2] - estimated_corrs[group_1]
					observed_corr_diff = self.parameters[group_2]['corr'][gene_idx_1, gene_idx_2] - self.parameters[group_1]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['corr_1'][gene_idx_1, gene_idx_2]  = self.parameters[group_1]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['corr_2'][gene_idx_1, gene_idx_2]  = self.parameters[group_2]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['dcorr_diff'][gene_idx_1, gene_idx_2]  = observed_corr_diff

					if np.isfinite(observed_corr_diff):
						hypothesis_test_dict[(group_1, group_2)]['dcorr_pval'][gene_idx_1, gene_idx_2] = compute_asl(boot_corr_diff)
					if np.isfinite(observed_cov_diff):
						hypothesis_test_dict[(group_1, group_2)]['dcov_pval'][gene_idx_1, gene_idx_2] = compute_asl(boot_cov_diff)

				iter_2 += 1
			iter_1 += 1

		# Perform FDR correction
		for group_1, group_2 in comparison_groups:
			hypothesis_test_dict[(group_1, group_2)]['cov_1'] = hypothesis_test_dict[(group_1, group_2)]['cov_1'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['cov_2'] = hypothesis_test_dict[(group_1, group_2)]['cov_2'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dcov_diff'] = hypothesis_test_dict[(group_1, group_2)]['dcov_diff'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dcov_fdr'] = fdrcorrect(hypothesis_test_dict[(group_1, group_2)]['dcov_pval'][genes_idxs_1, :][:, genes_idxs_2].reshape(-1))\
				.reshape(genes_idxs_1.shape[0], genes_idxs_2.shape[0])
			hypothesis_test_dict[(group_1, group_2)]['dcov_pval'] = hypothesis_test_dict[(group_1, group_2)]['dcov_pval'][genes_idxs_1, :][:, genes_idxs_2]

			hypothesis_test_dict[(group_1, group_2)]['corr_1'] = hypothesis_test_dict[(group_1, group_2)]['corr_1'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['corr_2'] = hypothesis_test_dict[(group_1, group_2)]['corr_2'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dcorr_diff'] = hypothesis_test_dict[(group_1, group_2)]['dcorr_diff'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dcorr_fdr'] = fdrcorrect(hypothesis_test_dict[(group_1, group_2)]['dcorr_pval'][genes_idxs_1, :][:, genes_idxs_2].reshape(-1))\
				.reshape(genes_idxs_1.shape[0], genes_idxs_2.shape[0])
			hypothesis_test_dict[(group_1, group_2)]['dcorr_pval'] = hypothesis_test_dict[(group_1, group_2)]['dcorr_pval'][genes_idxs_1, :][:, genes_idxs_2]

		# Update the attribute dictionaries
		for group in groups_to_iter:
			if group in self.parameters_confidence_intervals:
				self.parameters_confidence_intervals[group].update(ci_dict[group])
			else:
				self.parameters_confidence_intervals[group] = ci_dict[group]
		self.hypothesis_test_result_2d.update(hypothesis_test_dict)

