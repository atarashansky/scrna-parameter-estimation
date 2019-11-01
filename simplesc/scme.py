"""
	scme.py

	Single Cell Moment Estimator

	This file contains code for implementing the empirical bayes estimator for the Gaussian assumption for true single cell RNA sequencing counts.
"""


import pandas as pd
import scipy.stats as stats
import numpy as np
import time
import itertools
import scipy as sp
import logging
from scipy.stats import multivariate_normal
import pickle as pkl
from statsmodels.stats.moment_helpers import cov2corr
from statsmodels.stats.multitest import fdrcorrection
import sklearn.decomposition as decomp
import matplotlib
matplotlib.use('AGG')
import matplotlib.pyplot as plt


def compute_asl(perm_diff):
	""" 
		Use the generalized pareto distribution to model the tail of the permutation distribution. 
	"""

	extreme_count = (perm_diff > 0).sum()
	extreme_count = min(extreme_count, perm_diff.shape[0] - extreme_count)

	if extreme_count > 2: # We do not need to use the GDP approximation. 

		return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))

	else: # We use the GDP approximation

		perm_mean = perm_diff.mean()
		perm_dist = np.sort(perm_diff) if perm_mean < 0 else np.sort(-perm_diff) # For fitting the GDP later on
		N_exec = 300 # Starting value for number of exceendences

		while N_exec > 50:

			tail_data = perm_dist[-N_exec:]
			params = stats.genextreme.fit(tail_data)
			_, ks_pval = stats.kstest(tail_data, 'genextreme', args=params)

			if ks_pval > 0.05: # roughly a genpareto distribution
				return 2 * (N_exec/perm_diff.shape[0]) * stats.genextreme.sf(1, *params)
			else: # Failed to fit genpareto
				N_exec -= 30

		# Failed to fit genpareto, return the upper bound
		return 2 * ((extreme_count + 1) / (perm_diff.shape[0] + 1))


def pair(k1, k2, safe=True):
    """
    Cantor pairing function
    http://en.wikipedia.org/wiki/Pairing_function#Cantor_pairing_function
    """
    z = (0.5 * (k1 + k2) * (k1 + k2 + 1) + k2).astype(int)
    return z


def depair(z):
    """
    Inverse of Cantor pairing function
    http://en.wikipedia.org/wiki/Pairing_function#Inverting_the_Cantor_pairing_function
    """
    w = np.floor((np.sqrt(8 * z + 1) - 1)/2)
    t = (w**2 + w) / 2
    y = (z - t).astype(int)
    x = (w - y).astype(int)
    return x, y


def robust_linregress(a, b):
	""" Wrapper for scipy linregress function that ignores non-finite values. """

	condition = (np.isfinite(a) & np.isfinite(b))
	x = a[condition]
	y = b[condition]

	return stats.linregress(x,y)


def fdrcorrect(pvals):
	"""
		Perform FDR correction with nan's.
	"""

	fdr = np.ones(pvals.shape[0])
	_, fdr[~np.isnan(pvals)] = fdrcorrection(pvals[~np.isnan(pvals)])
	return fdr


class SingleCellEstimator(object):
	"""
		SingleCellEstimator is the class for fitting univariate and bivariate single cell data. 

		:beta: Expected value of the capture rate.
	"""


	def __init__(
		self, 
		adata,
		group_label,
		n_umis_column,
		leave_one_out=False,
		num_permute=10000,
		beta=0.1):

		self.anndata = adata.copy()
		self.genes = adata.var.index.values
		self.barcodes = adata.obs.index
		self.beta = beta
		self.group_label = group_label
		self.groups = self.anndata.obs[self.group_label].drop_duplicates().tolist()
		self.group_counts = dict(adata.obs[group_label].value_counts())
		self.leave_one_out = leave_one_out
		if leave_one_out:
			for group in list(self.group_counts.keys()):
				self.group_counts['-' + group] = self.anndata.shape[0] - self.group_counts[group]
		self.n_umis = adata.obs[n_umis_column].values
		self.num_permute = num_permute

		# Initialize parameter containing dictionaries
		self.observed_moments = {}
		self.observed_central_moments = {}
		self.estimated_moments = {}
		self.estimated_central_moments = {}
		self.parameters = {}
		self.parameters_confidence_intervals = {}

		# dictionaries for hypothesis testing
		self.hypothesis_test_result_1d = {}
		self.hypothesis_test_result_2d = {}


	def _select_cells(self, group):
		""" Select the cells. """

		if group == 'all': # All cells
			cell_selector = np.arange(self.anndata.shape[0])
		elif group[0] == '-': # Exclude this group
			cell_selector = (self.anndata.obs[self.group_label] != group[1:]).values
		else: # Include this group
			cell_selector = (self.anndata.obs[self.group_label] == group).values

		return cell_selector


	def _compute_statistics(self, observed, N):
		""" Compute some non central moments of the observed data. """

		# Turn the highest value into a 0
		# observed[observed == observed.max()] = 0

		if type(observed) != np.ndarray: # Some sparse matrix

			mean = observed.mean(axis=0).A1
			cov = ((observed.T*observed -(sum(observed).T*sum(observed)/N))/(N-1)).todense()

		else: # Numpy ndarray

			mean = observed.mean(axis=0)
			cov = np.cov(observed, rowvar=False)

		prod_expect = cov + mean.reshape(-1,1).dot(mean.reshape(1, -1))

		# Return the first moment, second moment, expectation of the product
		return mean, np.diag(prod_expect), prod_expect


	def _estimate_mean(self, observed_first):
		""" Estimate the mean vector. """

		return observed_first/self.beta


	def _estimate_variance(self, observed_first, observed_second, mean_inv_allgenes):
		""" Estimate the true variance. """

		numer = observed_first - (self.beta_sq/self.beta)*observed_first - observed_second
		denom = -self.beta_sq + self.beta*mean_inv_allgenes - self.beta_sq*mean_inv_allgenes

		return numer/denom - observed_first**2/self.beta**2


	def _estimate_covariance(self, observed_first, observed_second, observed_prod, mean_inv_allgenes):
		""" Estimate the true covariance. """

		# Estimate covariances except for the diagonal
		denom = self.beta_sq - (self.beta - self.beta_sq)*mean_inv_allgenes
		cov = observed_prod / denom - observed_first.reshape(-1,1)@observed_first.reshape(1, -1)/self.beta**2

		# Estimate the variance
		var = self._estimate_variance(observed_first, observed_second, mean_inv_allgenes)

		cov[np.diag_indices_from(cov)] = var
		return var, cov


	def _estimate_residual_variance(self, estimated_mean, estimated_var):
		""" 
			Estimate the residual variance by performing linear regression. 

			Returns the residual variance as well as its logarithm.
		"""

		log_residual_variance = np.log(estimated_var) - (self.mean_var_slope*np.log(estimated_mean) + self.mean_var_inter)

		return np.exp(log_residual_variance), log_residual_variance


	def estimate_beta_sq(self, tolerance=5):
		""" 
			Estimate the expected value of the square of the capture rate beta. 

			Estimate the relationship between mean and variance. This means that we assume the mean variance relationship to be 
			the same for all cell types. 
		"""

		# Compute observed moments in every group
		groups = self.anndata.obs[self.group_label].drop_duplicates()
		for group in self.groups:

			print('Computing observed moments for:', group)
			self.compute_observed_moments(group)
			#self.compute_observed_moments('-' + group) This is the leave one out part.

		# Combine all observed moments from every group
		x = np.concatenate([self.observed_central_moments[group]['first'] for group in self.groups])
		y = np.concatenate([self.observed_central_moments[group]['second'] for group in self.groups])

		# Filter for finite estimates
		condition = np.isfinite(x) & np.isfinite(y)
		x = x[condition]
		y = y[condition]

		# Estimate the noise level, or CV^2_beta + 1
		noise_level = np.percentile(
			(y/x**2 - 1/x)[y > x], 
			q=tolerance)

		self.all_group_obs_means = x
		self.all_group_obs_var = y
		self.all_group_obs_cv_sq = y/x**2
		self.noise_level = noise_level
		self.beta_sq = (noise_level+1)*self.beta**2


	def plot_cv_mean_curve(self):
		"""
			Plot the observed characteristic relationship between the mean and the coefficient of variation. 

			If an estimate for beta_sq exists, also plot the estimated baseline noise level.
		"""

		plt.figure(figsize=(5.5, 5))
		obs_mean = self.all_group_obs_means
		obs_cv = self.all_group_obs_cv_sq

		plt.scatter(
		    np.log(obs_mean),
		    np.log(obs_cv),
		    s=2
		)

		bound_x = np.arange(
		    np.nanmin(obs_mean),
		    np.nanmax(obs_mean),
		    0.01)
		bound_y = 1/bound_x + self.noise_level

		plt.plot(np.log(bound_x), -np.log(bound_x), color='k', lw=2)
		plt.plot(np.log(bound_x), np.log(bound_y), lw=2, color='r')
		plt.axis('equal');
		plt.legend(['Poisson', 'Poisson + noise', 'genes'])
		plt.title('Observed Mean - CV Relationship');
		plt.xlabel('log( observed mean )')
		plt.ylabel('log( observed CV^2 )')


	def estimate_parameters(self):
		""" Perform parameter estimation. """

		# Compute estimated moments
		for group in self.group_counts:
			self.compute_estimated_moments(group)

		# Combine all estimated moments from every group
		x = np.concatenate([self.estimated_central_moments[group]['first'] for group in self.groups])
		y = np.concatenate([self.estimated_central_moments[group]['second'] for group in self.groups])

		# Filter for finite estimates
		condition = np.isfinite(x) & np.isfinite(y)
		x = x[condition]
		y = y[condition]

		# Estimate the mean-var relationship
		slope, inter, _, _, _ = robust_linregress(np.log(x), np.log(y))
		self.mean_var_slope = slope
		self.mean_var_inter = inter

		# Compute final parameters
		for group in self.group_counts:
			self.compute_params(group)


	def compute_observed_moments(self, group='all'):
		""" Compute the observed statistics. """

		cell_selector = self._select_cells(group)

		observed = self.anndata.X[cell_selector, :].copy()
		N = observed.shape[0]

		first, second, prod = self._compute_statistics(observed, N)
		allgenes_first = self.n_umis[cell_selector].mean()
		allgenes_second = (self.n_umis[cell_selector]**2).mean()

		self.observed_moments[group] = {
			'first':first,
			'second':second,
			'prod':prod,
			'allgenes_first':allgenes_first,
			'allgenes_second':allgenes_second,
		}

		self.observed_central_moments[group] = {
			'first':first,
			'second':second-first**2,
			'prod':prod - first.reshape(-1,1).dot(first.reshape(1, -1)),
			'allgenes_first':allgenes_first,
			'allgenes_second':allgenes_second - allgenes_first**2
		}


	def compute_estimated_moments(self, group='all'):
		""" Use the observed moments to compute the moments of the underlying distribution. """

		mean_inv_numis = self.beta * self.observed_moments[group]['allgenes_second'] / self.observed_moments[group]['allgenes_first']**3

		estimated_mean = self._estimate_mean(self.observed_moments[group]['first'])

		estimated_var, estimated_cov = self._estimate_covariance(
			self.observed_moments[group]['first'],
			self.observed_moments[group]['second'],
			self.observed_moments[group]['prod'],
			mean_inv_numis)

		self.estimated_central_moments[group] = {
			'first': estimated_mean,
			'second': estimated_var,
			'prod': estimated_cov
		}


	def compute_params(self, group='all'):
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
			'corr': cov2corr(self.estimated_central_moments[group]['prod'])}


	def compute_confidence_intervals_1d(self, groups=None, groups_to_compare=None):
		"""
			Compute 95% confidence intervals around the estimated parameters. 

			Use the Gamma -> Dirichlet framework to speed up the process.

			Uses self.num_permute attribute, same as in hypothesis testing.

			CAVEAT: Uses the same expectation of 1/N as the true value, does not compute this from the permutations. 
			So the result might be slightly off.
		"""

		groups_to_iter = self.group_counts.keys() if groups is None else groups
		comparison_groups = groups_to_compare if groups_to_compare else list(itertools.combinations(groups_to_iter, 2))

		mean_inv_numis = {group:(self.beta * self.observed_moments[group]['allgenes_second'] / self.observed_moments[group]['allgenes_first']**3) \
			for group in groups_to_iter}

		all_counts = {group:set() for group in groups_to_iter}
		gene_counts = {}

		# Collect all unique counts in this group
		for gene_idx in range(self.anndata.var.shape[0]):

			gene_counts[gene_idx] = {}

			for group in groups_to_iter:

				cell_selector = self._select_cells(group)
				data = self.anndata.X[cell_selector, :].toarray() if type(self.anndata.X) != np.ndarray else self.anndata.X[cell_selector, :]

				expr_values, counts = np.unique(data[:, gene_idx].reshape(-1).astype(int), return_counts=True)

				gene_counts[gene_idx][group] = (expr_values, counts)

				all_counts[group] |= set(counts)

		# All unique counts from all groups across all genes
		all_counts_sorted = {group:np.array(sorted(list(counts))) for group,counts in all_counts.items()}

		# Define the gamma variales to be later used to construct the Dirichlet
		gamma_rvs = {group:stats.gamma.rvs(
			a=(counts+1e-10), 
			size=(self.num_permute, counts.shape[0])) for group, counts in all_counts_sorted.items()}

		print('Gamma RVs generated..')

		# Declare placeholders for gene confidence intervals
		parameters = ['mean', 'residual_var', 'log_mean', 'log_residual_var', 'log1p_mean', 'log1p_residual_var']
		ci_dict = {
			group:{param:np.zeros(self.anndata.var.shape[0]) for param in parameters} \
				for group in groups_to_iter}

		# Declare placeholders for group comparison results
		hypothesis_test_parameters = [
			'log_mean_1', 'log_mean_2', 
			'log_residual_var_1', 'log_residual_var_2', 
			'de_diff', 'de_pval', 
			'de_fdr', 'dv_diff', 
			'dv_pval', 'dv_fdr']
		hypothesis_test_dict = {
			(group_1, group_2):{param:np.zeros(self.anndata.var.shape[0]) for param \
				in hypothesis_test_parameters} for group_1, group_2 in comparison_groups}

		# Iterate through each gene and compute a standard error for each gene
		for gene_idx in range(self.anndata.var.shape[0]):

			# Grab the appropriate Gamma variables given the bincounts of this particular gene
			gene_gamma_rvs = {group:(gamma_rvs[group][:, np.nonzero(gene_counts[gene_idx][group][1][:, None] == all_counts_sorted[group])[1]]) \
				for group in groups_to_iter}

			# Sample dirichlet from the Gamma variables
			gene_dir_rvs = {group:(gene_gamma_rvs[group]/gene_gamma_rvs[group].sum(axis=1)[:,None]) for group in groups_to_iter}
			del gene_gamma_rvs

			# Construct the repeated values matrix
			values = {group:np.tile(
				gene_counts[gene_idx][group][0].reshape(1, -1), (self.num_permute, 1)) for group in groups_to_iter}

			# Compute the permuted, observed mean/dispersion
			mean = {group:((gene_dir_rvs[group]) * values[group]).sum(axis=1) for group in groups_to_iter}
			second_moments = {group:((gene_dir_rvs[group]) * values[group]**2).sum(axis=1) for group in groups_to_iter}
			del gene_dir_rvs

			# Compute the permuted, estimated moments for both groups
			estimated_means = {group:self._estimate_mean(mean[group]) for group in groups_to_iter}
			estimated_vars = {group:self._estimate_variance(mean[group], second_moments[group], mean_inv_numis[group]) for group in groups_to_iter}
			estimated_residual_vars = {group:self._estimate_residual_variance(estimated_means[group], estimated_vars[group])[0] for group in groups_to_iter}

			# Store the S.E. of the parameter, log(param), and log1p(param)
			for group in groups_to_iter:

				ci_dict[group]['mean'][gene_idx] = np.nanstd(estimated_means[group])
				ci_dict[group]['residual_var'][gene_idx] = np.nanstd(estimated_residual_vars[group])
				ci_dict[group]['log_mean'][gene_idx] = np.nanstd(np.log(estimated_means[group]))
				ci_dict[group]['log_residual_var'][gene_idx] = np.nanstd(np.log(estimated_residual_vars[group]))
				ci_dict[group]['log1p_mean'][gene_idx] = np.nanstd(np.log(estimated_means[group]+1))
				ci_dict[group]['log1p_residual_var'][gene_idx] = np.nanstd(np.log(estimated_residual_vars[group]+1))

			# Perform hypothesis testing
			for group_1, group_2 in comparison_groups:

				# For difference of log means
				boot_log_mean_diff = np.log(estimated_means[group_2]) - np.log(estimated_means[group_1])
				observed_log_mean_diff = self.parameters[group_2]['log_mean'][gene_idx] - self.parameters[group_1]['log_mean'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['log_mean_1'][gene_idx]  = self.parameters[group_1]['log_mean'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['log_mean_2'][gene_idx]  = self.parameters[group_2]['log_mean'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['de_diff'][gene_idx]  = observed_log_mean_diff
				if np.isfinite(observed_log_mean_diff):

					asl = compute_asl(boot_log_mean_diff)

					hypothesis_test_dict[(group_1, group_2)]['de_pval'][gene_idx] = asl
				else:
					hypothesis_test_dict[(group_1, group_2)]['de_pval'][gene_idx] = np.nan

				# For difference of log residual variances
				boot_log_residual_var_diff = np.log(estimated_residual_vars[group_2]) - np.log(estimated_residual_vars[group_1])
				observed_log_residual_var_diff = self.parameters[group_2]['log_residual_var'][gene_idx] - self.parameters[group_1]['log_residual_var'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['log_residual_var_1'][gene_idx]  = self.parameters[group_1]['log_residual_var'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['log_residual_var_2'][gene_idx]  = self.parameters[group_2]['log_residual_var'][gene_idx]
				hypothesis_test_dict[(group_1, group_2)]['dv_diff'][gene_idx]  = observed_log_residual_var_diff
				if np.isfinite(observed_log_residual_var_diff):
					asl = compute_asl(boot_log_residual_var_diff)
					hypothesis_test_dict[(group_1, group_2)]['dv_pval'][gene_idx] = asl
				else:
					hypothesis_test_dict[(group_1, group_2)]['dv_pval'][gene_idx] = np.nan

		# Perform FDR correction
		for group_1, group_2 in comparison_groups:

			hypothesis_test_dict[(group_1, group_2)]['de_fdr'] = fdrcorrect(hypothesis_test_dict[(group_1, group_2)]['de_pval'])
			hypothesis_test_dict[(group_1, group_2)]['dv_fdr'] = fdrcorrect(hypothesis_test_dict[(group_1, group_2)]['dv_pval'])

		# Update the attribute dictionaries
		self.parameters_confidence_intervals.update(ci_dict)
		self.hypothesis_test_result_1d.update(hypothesis_test_dict)
		

	def compute_confidence_intervals_2d(self, gene_list_1, gene_list_2, groups=None, groups_to_compare=None):
		"""
			Compute 95% confidence intervals around the estimated parameters. 

			Use the Gamma -> Dirichlet framework to speed up the process.

			Uses self.num_permute attribute, same as in hypothesis testing.

			CAVEAT: Uses the same expectation of 1/N as the true value, does not compute this from the permutations. 
			So the result might be slightly off.
		"""

		groups_to_iter = self.group_counts.keys() if groups is None else groups
		comparison_groups = groups_to_compare if groups_to_compare else list(itertools.combinations(groups_to_iter, 2))

		mean_inv_numis = {group:(self.beta * self.observed_moments[group]['allgenes_second'] / self.observed_moments[group]['allgenes_first']**3) \
			for group in groups_to_iter}

		# Get the gene idx for the two gene lists
		genes_idxs_1 = np.array([np.where(self.anndata.var.index == gene)[0][0] for gene in gene_list_1])
		genes_idxs_2 = np.array([np.where(self.anndata.var.index == gene)[0][0] for gene in gene_list_2])

		all_pair_counts = {group:set() for group in groups_to_iter}
		pair_counts = {}

		# Collect all unique counts in this group
		for gene_idx_1 in genes_idxs_1:

			pair_counts[gene_idx_1] = {}

			for gene_idx_2 in genes_idxs_2:

				if gene_idx_1 == gene_idx_2:
					continue

				pair_counts[gene_idx_1][gene_idx_2] = {}

				for group in groups_to_iter:

					cell_selector = self._select_cells(group)
					data = self.anndata.X[cell_selector, :].toarray() if type(self.anndata.X) != np.ndarray else self.anndata.X[cell_selector, :]

					cantor_code = pair(data[:, gene_idx_1], data[:, gene_idx_2])

					expr_values, counts = np.unique(cantor_code, return_counts=True)

					pair_counts[gene_idx_1][gene_idx_2][group] = (expr_values, counts)

					all_pair_counts[group] |= set(counts)

		# All unique counts from all groups across all genes
		all_pair_counts_sorted = {group:np.array(sorted(list(counts))) for group,counts in all_pair_counts.items()}

		# Define the gamma variales to be later used to construct the Dirichlet
		gamma_rvs = {group:stats.gamma.rvs(
			a=(counts+1e-10), 
			size=(self.num_permute, counts.shape[0])) for group, counts in all_pair_counts_sorted.items()}

		print('Gamma RVs generated..')

		# Declare placeholders for gene confidence intervals
		parameters = ['corr']
		ci_dict = {
			group:{param:np.zeros(self.parameters[groups_to_iter[0]]['corr'].shape)*np.nan for param in parameters} \
				for group in groups_to_iter}

		# Declare placeholders for group comparison results
		hypothesis_test_parameters = ['dc_diff', 'dc_pval', 'dc_fdr', 'corr_1', 'corr_2']
		hypothesis_test_dict = {
			(group_1, group_2):{param:np.zeros(self.parameters[group_1]['corr'].shape)*np.nan for param \
				in hypothesis_test_parameters} for group_1, group_2 in comparison_groups}
		for group_1, group_2 in comparison_groups:
			hypothesis_test_dict[(group_1, group_2)]['gene_idx_1'] = genes_idxs_1
			hypothesis_test_dict[(group_1, group_2)]['gene_idx_2'] = genes_idxs_2

		# Iterate through each gene and compute a standard error for each gene
		for gene_idx_1 in genes_idxs_1:

			for gene_idx_2 in genes_idxs_2:

				if gene_idx_2 == gene_idx_1:
					continue

				# Grab the appropriate Gamma variables given the bincounts of this particular gene
				gene_gamma_rvs = {group:(gamma_rvs[group][:, np.nonzero(pair_counts[gene_idx_1][gene_idx_2][group][1][:, None] == all_pair_counts_sorted[group])[1]]) \
					for group in groups_to_iter}

				# Sample dirichlet from the Gamma variables
				gene_dir_rvs = {group:(gene_gamma_rvs[group]/gene_gamma_rvs[group].sum(axis=1)[:,None]) for group in groups_to_iter}
				del gene_gamma_rvs

				# Construct the repeated values matrix
				cantor_code = {group:pair_counts[gene_idx_1][gene_idx_2][group][0] for group in groups_to_iter}
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
				estimated_means_1 = {group:self._estimate_mean(mean_1[group]) for group in groups_to_iter}
				estimated_vars_1 = {group:self._estimate_variance(mean_1[group], second_moments_1[group], mean_inv_numis[group]) for group in groups_to_iter}
				estimated_means_2 = {group:self._estimate_mean(mean_2[group]) for group in groups_to_iter}
				estimated_vars_2 = {group:self._estimate_variance(mean_2[group], second_moments_2[group], mean_inv_numis[group]) for group in groups_to_iter}

				# Compute estimated correlations
				estimated_corrs = {}
				estimated_covs = {}
				for group in groups_to_iter:
					denom = self.beta_sq - (self.beta - self.beta_sq)*mean_inv_numis[group]
					cov = prod[group] / denom - (mean_1[group] * mean_2[group])/self.beta**2
					estimated_covs[group] = cov
					estimated_corrs[group] = cov / np.sqrt(estimated_vars_1[group]*estimated_vars_2[group]) 

				# Store the S.E. of the correlation
				for group in groups_to_iter:
					ci_dict[group]['corr'][gene_idx_1, gene_idx_2] = np.nanstd(estimated_corrs[group])

				# Perform hypothesis testing
				for group_1, group_2 in comparison_groups:

					# For difference of log means
					boot_corr_diff = estimated_corrs[group_2] - estimated_corrs[group_1]
					observed_corr_diff = self.parameters[group_2]['corr'][gene_idx_1, gene_idx_2] - self.parameters[group_1]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['corr_1'][gene_idx_1, gene_idx_2]  = self.parameters[group_1]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['corr_2'][gene_idx_1, gene_idx_2]  = self.parameters[group_2]['corr'][gene_idx_1, gene_idx_2]
					hypothesis_test_dict[(group_1, group_2)]['dc_diff'][gene_idx_1, gene_idx_2]  = observed_corr_diff

					if np.isfinite(observed_corr_diff):

						asl = compute_asl(boot_corr_diff)
						hypothesis_test_dict[(group_1, group_2)]['dc_pval'][gene_idx_1, gene_idx_2] = asl

		# Perform FDR correction
		for group_1, group_2 in comparison_groups:

			hypothesis_test_dict[(group_1, group_2)]['corr_1'] = hypothesis_test_dict[(group_1, group_2)]['corr_1'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['corr_2'] = hypothesis_test_dict[(group_1, group_2)]['corr_2'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dc_diff'] = hypothesis_test_dict[(group_1, group_2)]['dc_diff'][genes_idxs_1, :][:, genes_idxs_2]
			hypothesis_test_dict[(group_1, group_2)]['dc_fdr'] = fdrcorrect(hypothesis_test_dict[(group_1, group_2)]['dc_pval'][genes_idxs_1, :][:, genes_idxs_2].reshape(-1))\
				.reshape(genes_idxs_1.shape[0], genes_idxs_2.shape[0])
			hypothesis_test_dict[(group_1, group_2)]['dc_pval'] = hypothesis_test_dict[(group_1, group_2)]['dc_pval'][genes_idxs_1, :][:, genes_idxs_2]

		# Update the attribute dictionaries
		for group in groups_to_iter:
			if group in self.parameters_confidence_intervals:
				self.parameters_confidence_intervals[group].update(ci_dict[group])
			else:
				self.parameters_confidence_intervals[group] = ci_dict[group]
		self.hypothesis_test_result_2d.update(hypothesis_test_dict)

