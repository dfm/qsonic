"""Continuum fitting module."""
import argparse

import numpy as np
import fitsio
from scipy.optimize import minimize, curve_fit
from scipy.interpolate import UnivariateSpline
from scipy.special import legendre

from mpi4py import MPI

from qsonic.spectrum import valid_spectra
from qsonic.mpi_utils import logging_mpi, warn_mpi, MPISaver
from qsonic.mathtools import mypoly1d, Fast1DInterpolator, SubsampleCov


def add_picca_continuum_parser(parser=None):
    """ Adds PiccaContinuumFitter related arguments to parser. These
    arguments are grouped under 'Continuum fitting options'. All of them
    come with defaults, none are required.

    Arguments
    ---------
    parser: argparse.ArgumentParser, default: None

    Returns
    ---------
    parser: argparse.ArgumentParser
    """
    if parser is None:
        parser = argparse.ArgumentParser(
            formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    cont_group = parser.add_argument_group('Continuum fitting options')

    cont_group.add_argument(
        "--rfdwave", type=float, default=0.8,
        help="Rest-frame wave steps. Complies with forest limits")
    cont_group.add_argument(
        "--no-iterations", type=int, default=5,
        help="Number of iterations for continuum fitting.")
    cont_group.add_argument(
        "--fiducial-meanflux", help="Fiducial mean flux FITS file.")
    cont_group.add_argument(
        "--fiducial-varlss", help="Fiducial var_lss FITS file.")
    cont_group.add_argument(
        "--cont-order", type=int, default=1,
        help="Order of continuum fitting polynomial.")

    return parser


class PiccaContinuumFitter():
    """ Picca continuum fitter class.

    Fits spectra without coadding. Pipeline ivar is smoothed before using in
    weights. Mean continuum is smoothed using inverse weights and cubic spline.
    Contruct, then call `iterate()` with local spectra.

    Parameters
    ----------
    args: argparse.Namespace
        Namespace. Wavelength values are taken to be the edges (not centers).
        See respective parsers for default values.
    nwbins: int
        Number of wavelength bins in the observed frame for variance fitting.

    Attributes
    ----------
    nbins: int
        Number of bins for the mean continuum in the rest frame.
    rfwave: :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Rest-frame wavelength centers for the mean continuum.
    _denom: float
        Denominator for the slope term in the continuum model.
    meancont_interp: Fast1DInterpolator
        Fast linear interpolator object for the mean continuum.
    comm: MPI.COMM_WORLD
        MPI comm object to reduce, broadcast etc.
    mpi_rank: int
        Rank of the MPI process.
    meanflux_interp: Fast1DInterpolator
        Interpolator for mean flux. If fiducial is not set, this equals to 1.
    varlss_fitter: VarLSSFitter or None
        None if fiducials are set for var_lss.
    varlss_interp: Fast1DInterpolator
        Interpolator for var_lss.
    niterations: int
        Number of iterations from `args.no_iterations`.
    cont_order: int
        Order of continuum polynomial from `args.cont_order`.
    outdir: str or None
        Directory to save catalogs. If None or empty, does not save.
    """

    def _get_fiducial_interp(self, fname, col2read):
        """ Return an interpolator for mean flux or var_lss.

        FITS file must have a 'STATS' extention, which must have 'LAMBDA',
        'MEANFLUX' and 'VAR' columns. This is the same format as raw_io output
        from picca. 'LAMBDA' must be linearly and equally spaced.
        This function sets up ``col2read`` as Fast1DInterpolator object.

        Arguments
        ---------
        fname: str
            Filename of the FITS file.
        col2read: str
            Should be 'MEANFLUX' or 'VAR'.

        Returns
        -------
        Fast1DInterpolator
        """
        if self.mpi_rank == 0:
            with fitsio.FITS(fname) as fts:
                data = fts['STATS'].read()

            waves = data['LAMBDA']
            waves_0 = waves[0]
            dwave = waves[1] - waves[0]
            nsize = waves.size

            if not np.allclose(np.diff(waves), dwave):
                # Set nsize to 0, later will be used to diagnose and exit
                # for uneven wavelength array.
                nsize = 0
            elif col2read not in data.dtype.names:
                nsize = -1
            else:
                data = np.array(data[col2read], dtype='d')
        else:
            waves_0 = 0.
            dwave = 0.
            nsize = 0

        nsize, waves_0, dwave = self.comm.bcast([nsize, waves_0, dwave])

        if nsize == 0:
            raise Exception(
                "Failed to construct fiducial mean flux or varlss from "
                f"{fname}::LAMBDA is not equally spaced.")

        if nsize == -1:
            raise Exception(
                "Failed to construct fiducial mean flux or varlss from "
                f"{fname}::{col2read} is not in file.")

        if self.mpi_rank != 0:
            data = np.empty(nsize, dtype='d')

        self.comm.Bcast([data, MPI.DOUBLE])

        return Fast1DInterpolator(waves_0, dwave, data, ep=np.zeros(nsize))

    def __init__(self, args, nwbins=20):
        # We first decide how many bins will approximately satisfy
        # rest-frame wavelength spacing. Then we create wavelength edges, and
        # transform these edges into centers
        self.nbins = int(round(
            (args.forest_w2 - args.forest_w1) / args.rfdwave))
        edges, self.dwrf = np.linspace(
            args.forest_w1, args.forest_w2, self.nbins + 1, retstep=True)
        self.rfwave = (edges[1:] + edges[:-1]) / 2
        self._denom = np.log(self.rfwave[-1] / self.rfwave[0])

        self.meancont_interp = Fast1DInterpolator(
            self.rfwave[0], self.dwrf, np.ones(self.nbins),
            ep=np.zeros(self.nbins))

        self.comm = MPI.COMM_WORLD
        self.mpi_rank = self.comm.Get_rank()

        if args.fiducial_meanflux:
            self.meanflux_interp = self._get_fiducial_interp(
                args.fiducial_meanflux, 'MEANFLUX')
        else:
            self.meanflux_interp = Fast1DInterpolator(0., 1., np.ones(3))

        if args.fiducial_varlss:
            self.varlss_fitter = None
            self.varlss_interp = self._get_fiducial_interp(
                args.fiducial_varlss, 'VAR')
        else:
            self.varlss_fitter = VarLSSFitter(
                args.wave1, args.wave2, nwbins,
                comm=self.comm)
            self.varlss_interp = Fast1DInterpolator(
                self.varlss_fitter.waveobs[0], self.varlss_fitter.dwobs,
                0.1 * np.ones(nwbins), ep=np.zeros(nwbins))

        self.niterations = args.no_iterations
        self.cont_order = args.cont_order
        self.outdir = args.outdir

    def _continuum_costfn(self, x, wave, flux, ivar_sm, z_qso):
        """ Cost function to minimize for each quasar.

        This is a modified chi2 where amplitude is also part of minimization.
        Cost of each arm is simply added to the total cost.

        Arguments
        ---------
        x: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Polynomial coefficients for quasar diversity.
        wave: dict(:external+numpy:py:class:`ndarray <numpy.ndarray>`)
            Observed-frame wavelengths.
        flux: dict(:external+numpy:py:class:`ndarray <numpy.ndarray>`)
            Flux.
        ivar_sm: dict(:external+numpy:py:class:`ndarray <numpy.ndarray>`)
            Smooth inverse variance.
        z_qso: float
            Quasar redshift.

        Returns
        ---------
        cost: float
            Cost (modified chi2) for a given `x`.
        """
        cost = 0

        for arm, wave_arm in wave.items():
            cont_est = self.get_continuum_model(x, wave_arm / (1 + z_qso))
            # no_neg = np.sum(cont_est<0)
            # penalty = wave_arm.size * no_neg**2

            cont_est *= self.meanflux_interp(wave_arm)

            var_lss = self.varlss_interp(wave_arm) * cont_est**2
            weight = ivar_sm[arm] / (1 + ivar_sm[arm] * var_lss)
            w = weight > 0

            cost += np.dot(
                weight, (flux[arm] - cont_est)**2
            ) - np.log(weight[w]).sum()  # + penalty

        return cost

    def get_continuum_model(self, x, wave_rf_arm):
        """ Returns interpolated continuum model.

        Arguments
        ---------
        x: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Polynomial coefficients for quasar diversity.
        wave_rf_arm: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Rest-frame wavelength per arm.

        Returns
        ---------
        cont: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Continuum at `wave_rf_arm` values given `x`.
        """
        slope = np.log(wave_rf_arm / self.rfwave[0]) / self._denom

        cont = self.meancont_interp(wave_rf_arm) * mypoly1d(x, 2 * slope - 1)
        # Multiply with resolution
        # Edges are difficult though

        return cont

    def fit_continuum(self, spec):
        """ Fits the continuum for a single Spectrum.

        This function uses
        :attr:`forestivar_sm <qsonic.spectrum.Spectrum.forestivar_sm>` in
        inverse variance, which must be smoothed beforehand.
        It also modifies
        :attr:`cont_params <qsonic.spectrum.Spectrum.cont_params>`
        dictionary's ``valid, cont, x, xcov, chi2, dof`` keys.
        If the best-fitting continuum is **negative at any point**, the fit is
        **invalidated**. Chi2 is set separately without using the
        :meth:`cost function <._continuum_costfn>`.
        ``x`` key is the best-fitting parameter, and ``xcov`` is their inverse
        Hessian ``hess_inv`` given by
        :external+scipy:func:`scipy.optimize.minimize` using 'L-BFGS-B' method.

        Arguments
        ---------
        spec: Spectrum
            Spectrum object to fit.
        """
        # We can precalculate meanflux and varlss here,
        # and store them in respective keys to spec.cont_params
        result = minimize(
            self._continuum_costfn,
            spec.cont_params['x'],
            args=(spec.forestwave,
                  spec.forestflux,
                  spec.forestivar_sm,
                  spec.z_qso),
            method='L-BFGS-B',
            bounds=None,
            jac=None
        )

        spec.cont_params['valid'] = result.success

        if result.success:
            spec.cont_params['cont'] = {}
            chi2 = 0
            for arm, wave_arm in spec.forestwave.items():
                cont_est = self.get_continuum_model(
                    result.x, wave_arm / (1 + spec.z_qso))

                if any(cont_est < 0):
                    spec.cont_params['valid'] = False
                    break

                cont_est *= self.meanflux_interp(wave_arm)
                spec.cont_params['cont'][arm] = cont_est
                var_lss = self.varlss_interp(wave_arm) * cont_est**2
                weight = 1. / (1 + spec.forestivar_sm[arm] * var_lss)
                weight *= spec.forestivar_sm[arm]

                chi2 += np.dot(weight, (spec.forestflux[arm] - cont_est)**2)

            # We can further eliminate spectra based chi2
            spec.cont_params['chi2'] = chi2

        if spec.cont_params['valid']:
            spec.cont_params['x'] = result.x
            spec.cont_params['xcov'] = result.hess_inv.todense()
        else:
            spec.cont_params['cont'] = None
            spec.cont_params['chi2'] = -1

    def fit_continua(self, spectra_list):
        """ Fits all continua for a list of Spectrum objects.

        Arguments
        ---------
        spectra_list: list(Spectrum)
            Spectrum objects to fit.
        """
        no_valid_fits = 0
        no_invalid_fits = 0

        # For each forest fit continuum
        for spec in spectra_list:
            self.fit_continuum(spec)

            if not spec.cont_params['valid']:
                no_invalid_fits += 1
            else:
                no_valid_fits += 1

        no_valid_fits = self.comm.reduce(no_valid_fits, root=0)
        no_invalid_fits = self.comm.reduce(no_invalid_fits, root=0)
        logging_mpi(f"Number of valid fits: {no_valid_fits}", self.mpi_rank)
        logging_mpi(f"Number of invalid fits: {no_invalid_fits}",
                    self.mpi_rank)

    def _project_normalize_meancont(self, new_meancont):
        """ Project out higher order Legendre polynomials from the new mean
        continuum, since these are degenerate with the free fitting parameters.
        Returns a normalized mean continuum. Integrals are calculated using
        ``np.trapz`` with ``ln lambda_RF`` as x array.

        Arguments
        ---------
        new_meancont: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            First estimate of the new mean continuum.

        Returns
        ---------
        new_meancont: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Legendere polynomials projected out and normalized mean continuum.
        mean: float
            Normalization of the mean continuum.
        """
        x = np.log(self.rfwave / self.rfwave[0]) / self._denom

        for ci in range(1, self.cont_order + 1):
            norm = 2 * ci + 1
            leg_ci = legendre(ci)(2 * x - 1)

            B = norm * np.trapz(new_meancont * leg_ci, x=x)
            new_meancont -= B * leg_ci

        # normalize
        mean = np.trapz(new_meancont, x=x)
        new_meancont /= mean

        return new_meancont, mean

    def update_mean_cont(self, spectra_list, noupdate):
        """ Update the global mean continuum.

        Uses :attr:`forestivar_sm <qsonic.spectrum.Spectrum.forestivar_sm>`
        in inverse variance, but must be set beforehand.
        Raw mean continuum estimates are smoothed with a weighted
        :external+scipy:py:class:`scipy.interpolate.UnivariateSpline`. The mean
        continuum is removed from higher Legendre polynomials and normalized by
        the mean. This function updates
        :attr:`meancont_interp.fp <.meancont_interp>` if noupdate is False.

        Arguments
        ---------
        spectra_list: list(Spectrum)
            Spectrum objects to fit.
        noupdate: bool
            Does not update :attr:`meancont_interp.fp <.meancont_interp>` if
            True (last iteration).

        Returns
        ---------
        has_converged: bool
            True if all continuum updates on every point are less than 0.33
            times the error estimates.
        """
        norm_flux = np.zeros(self.nbins)
        std_flux = np.empty(self.nbins)
        counts = np.zeros(self.nbins)

        for spec in valid_spectra(spectra_list):
            for arm, wave_arm in spec.forestwave.items():
                wave_rf_arm = wave_arm / (1 + spec.z_qso)
                bin_idx = (
                    (wave_rf_arm - self.rfwave[0]) / self.dwrf + 0.5
                ).astype(int)

                cont = spec.cont_params['cont'][arm]
                flux_ = spec.forestflux[arm] / cont
                # Deconvolve resolution matrix ?

                var_lss = self.varlss_interp(wave_arm)
                weight = spec.forestivar_sm[arm] * cont**2
                weight = weight / (1 + weight * var_lss)

                norm_flux += np.bincount(
                    bin_idx, weights=flux_ * weight, minlength=self.nbins)
                counts += np.bincount(
                    bin_idx, weights=weight, minlength=self.nbins)

        self.comm.Allreduce(MPI.IN_PLACE, norm_flux)
        self.comm.Allreduce(MPI.IN_PLACE, counts)
        w = counts > 0

        if w.sum() != self.nbins:
            warn_mpi(
                "Extrapolating empty bins in the mean continuum.",
                self.mpi_rank)

        norm_flux[w] /= counts[w]
        norm_flux[~w] = np.mean(norm_flux[w])
        std_flux[w] = 1 / np.sqrt(counts[w])
        std_flux[~w] = 10 * np.mean(std_flux[w])

        # Smooth new estimates
        spl = UnivariateSpline(self.rfwave, norm_flux, w=1 / std_flux)
        new_meancont = spl(self.rfwave)
        new_meancont *= self.meancont_interp.fp

        # remove tilt and higher orders and normalize
        new_meancont, mean_ = self._project_normalize_meancont(new_meancont)

        norm_flux = new_meancont / self.meancont_interp.fp - 1
        std_flux /= mean_

        if not noupdate:
            self.meancont_interp.fp = new_meancont
            self.meancont_interp.ep = std_flux

        all_pt_test = np.all(np.abs(norm_flux) < 0.33 * std_flux)
        chi2_change = np.sum((norm_flux / std_flux)**2) / self.nbins
        has_converged = (chi2_change < 1e-3) | all_pt_test

        if self.mpi_rank != 0:
            return has_converged

        text = ("Continuum updates\n" "rfwave\t| update\t| error\n")

        sl = np.s_[::max(1, int(self.nbins / 10))]
        for w, n, e in zip(self.rfwave[sl], norm_flux[sl], std_flux[sl]):
            text += f"{w:7.2f}\t| {n:7.2e}\t| pm {e:7.2e}\n"

        text += f"Change in chi2: {chi2_change*100:.4e}%"
        logging_mpi(text, 0)

        return has_converged

    def update_var_lss(self, spectra_list, noupdate):
        """ Fit for var_lss. See VarLSSFitter for implementation details.

        Arguments
        ---------
        spectra_list: list(Spectrum)
            Spectrum objects to fit.
        noupdate: bool
            Does not update `self.varlss_interp.fp` if True (last iteration).
        """
        if self.varlss_fitter is None:
            return

        self.varlss_fitter.reset()

        for spec in valid_spectra(spectra_list):
            for arm, wave_arm in spec.forestwave.items():
                cont = spec.cont_params['cont'][arm]
                delta = spec.forestflux[arm] / cont - 1
                ivar = spec.forestivar[arm] * cont**2

                self.varlss_fitter.add(wave_arm, delta, ivar)

        # Else, fit for var_lss
        logging_mpi("Fitting var_lss", self.mpi_rank)
        y, ep = self.varlss_fitter.fit(
            self.varlss_interp.fp)
        if not noupdate:
            self.varlss_interp.fp = y
            self.varlss_interp.ep = ep

        if self.mpi_rank != 0:
            return

        sl = np.s_[::max(1, int(y.size / 10))]
        text = "wave_obs \t| var_lss \t| error\n"
        for w, v, e in zip(self.varlss_fitter.waveobs[sl], y[sl], ep[sl]):
            text += f"{w:7.2f}\t| {v:7.2e} \t| {e:7.2e}\nnew"

        logging_mpi(text, 0)

    def iterate(self, spectra_list):
        """Main function to fit continua and iterate.

        Consists of three major steps: initializing, fitting, updating global
        variables. The initialization sets ``cont_params`` variable of every
        Spectrum object. Continuum polynomial order is carried by setting
        ``cont_params[x]``. At each iteration:

        1. Global variables (mean continuum, var_lss) are saved to file
           (attributes.fits) file. This ensures the order of what is used in
           each iteration.
        2. All spectra are fit.
        3. Mean continuum is updated by stacking, smoothing and removing
           degenarate modes. Check for convergence if update is small.
        4. If fitting for var_lss, fit and update by calculating variance
           statistics.

        At the end of requested iterations or convergence, a chi2 catalog is
        created that includes information regarding chi2, mean_snr, targetid,
        etc.

        Arguments
        ---------
        spectra_list: list(Spectrum)
            Spectrum objects to fit.
        """
        has_converged = False

        for spec in spectra_list:
            spec.cont_params['method'] = 'picca'
            spec.cont_params['x'] = np.append(
                spec.cont_params['x'][0], np.zeros(self.cont_order))
            spec.cont_params['xcov'] = np.eye(self.cont_order + 1)
            spec.cont_params['dof'] = spec.get_real_size()

        fname = f"{self.outdir}/attributes.fits" if self.outdir else ""
        fattr = MPISaver(fname, self.mpi_rank)

        for it in range(self.niterations):
            logging_mpi(
                f"Fitting iteration {it+1}/{self.niterations}", self.mpi_rank)

            self.save(fattr, it + 1)

            # Fit all continua one by one
            self.fit_continua(spectra_list)
            # Stack all spectra in each process
            # Broadcast and recalculate global functions
            has_converged = self.update_mean_cont(
                spectra_list, it == self.niterations - 1)

            self.update_var_lss(spectra_list, it == self.niterations - 1)

            if has_converged:
                logging_mpi("Iteration has converged.", self.mpi_rank)
                break

        if not has_converged:
            warn_mpi("Iteration has NOT converged.", self.mpi_rank)

        fattr.close()
        logging_mpi("All continua are fit.", self.mpi_rank)

        self.save_contchi2_catalog(spectra_list)

    def save(self, fattr, it):
        """Save mean continuum and var_lss (if fitting) to a fits file.

        Arguments
        ---------
        fattr: MPISaver
            File handler to save only on master node.
        it: int
            Current iteration number.
        """
        fattr.write(
            [self.rfwave, self.meancont_interp.fp, self.meancont_interp.ep],
            names=['lambda_rf', 'mean_cont', 'e_mean_cont'],
            extname=f'CONT-{it}')

        if self.varlss_fitter is None:
            return

        fattr.write(
            [self.varlss_fitter.waveobs, self.varlss_interp.fp,
             self.varlss_interp.ep],
            names=['lambda', 'var_lss', 'e_var_lss'], extname=f'VAR_FUNC-{it}')

    def save_contchi2_catalog(self, spectra_list):
        """Save chi2 catalog if ``self.outdir`` is set. All values are gathered
        and saved on the master node.

        Arguments
        ---------
        spectra_list: list(Spectrum)
            Spectrum objects to fit.
        """
        if not self.outdir:
            return

        logging_mpi("Saving continuum chi2 catalog.", self.mpi_rank)
        corder = self.cont_order + 1

        dtype = np.dtype([
            ('TARGETID', 'int64'), ('Z', 'f4'), ('HPXPIXEL', 'i8'),
            ('MPI_RANK', 'i4'), ('MEANSNR', 'f4'), ('RSNR', 'f4'),
            ('CONT_valid', bool), ('CONT_chi2', 'f4'), ('CONT_dof', 'i4'),
            ('CONT_x', 'f4', corder),
            ('CONT_xcov', 'f4', corder**2)
        ])
        local_catalog = np.empty(len(spectra_list), dtype=dtype)

        for i, spec in enumerate(spectra_list):
            row = local_catalog[i]
            row['TARGETID'] = spec.targetid
            row['Z'] = spec.z_qso
            row['HPXPIXEL'] = spec.catrow['HPXPIXEL']
            row['MPI_RANK'] = self.mpi_rank
            row['MEANSNR'] = spec.mean_snr()
            row['RSNR'] = spec.rsnr
            for lbl in ['valid', 'x', 'chi2', 'dof']:
                row[f'CONT_{lbl}'] = spec.cont_params[lbl]
            row['CONT_xcov'] = spec.cont_params['xcov'].ravel()

        all_catalogs = self.comm.gather(local_catalog)
        if self.mpi_rank == 0:
            all_catalog = np.concatenate(all_catalogs)
            fts = fitsio.FITS(
                f"{self.outdir}/continuum_chi2_catalog.fits", 'rw',
                clobber=True)
            fts.write(all_catalog, extname='CHI2_CAT')
            fts.close()


class VarLSSFitter(object):
    """ Variance fitter for the large-scale fluctuations.

    Input wavelengths and variances are the bin edges, so centers will be
    shifted. Valid bins require at least 500 pixels from 50 quasars. Assumes no
    spectra has `wave < w1obs` or `wave > w2obs`.

    .. note::

        This class is designed to be used in a linear fashion. You create it,
        add statistics to it and finally fit. After :meth:`fit` is called,
        :meth:`fit` and :meth:`add` **cannot** be called again. You may
        :meth:`reset` and start over.

    Usage::

        ...
        varfitter = VarLSSFitter(
            wave1, wave2, nwbins,
            var1, var2, nvarbins,
            nsubsamples=100, comm=comm)
        # Change static minimum numbers for valid statistics
        VarLSSFitter.min_no_pix = min_no_pix
        VarLSSFitter.min_no_qso = min_no_qso

        for delta in deltas_list:
            varfitter.add(delta.wave, delta.delta, delta.ivar)

        logging_mpi("Fitting variance for VarLSS and eta", mpi_rank)
        fit_results = np.ones((nwbins, 2))
        fit_results[:, 0] = 0.1
        fit_results, std_results = varfitter.fit(fit_results)

        varfitter.save("variance-file.fits")

        # You CANNOT call ``fit`` again!

    Parameters
    ----------
    w1obs: float
        Lower observed wavelength edge.
    w2obs: float
        Upper observed wavelength edge.
    nwbins: int
        Number of wavelength bins.
    var1: float, default: 1e-5
        Lower variance edge.
    var2: float, default: 2
        Upper variance edge.
    nvarbins: int, default: 100
        Number of variance bins.
    nsubsamples: int, default: 100
        Number of subsamples for the Jackknife covariance.
    comm: MPI.COMM_WORLD or None, default: None
        MPI comm object to allreduce if enabled.

    Attributes
    ----------
    waveobs: :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Wavelength centers in the observed frame.
    ivar_edges: :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Inverse variance edges.
    ivar_centers: :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Inverse variance centers.
    minlength: int
        Minimum size of the combined bin count array. It includes underflow and
        overflow bins for both wavelength and variance bins.
    var_delta_subs: SubsampleCov
        Variance delta.
    mean_delta_subs: SubsampleCov
        Mean delta.
    var2_delta_subs: SubsampleCov
        delta^4.
    num_pixels: int :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Number of pixels in the bin.
    num_qso: int :external+numpy:py:class:`ndarray <numpy.ndarray>`
        Number of quasars in the bin.
    comm: MPI.COMM_WORLD or None, default: None
        MPI comm object to allreduce if enabled.
    mpi_rank: int
        Rank of the MPI process if ``comm!=None``. Zero otherwise.
    """
    min_no_pix = 500
    """int: Minimum number of pixels a bin must have to be valid."""
    min_no_qso = 50
    """int: Minimum number of quasars a bin must have to be valid."""

    @staticmethod
    def variance_function(var_pipe, var_lss, eta=1):
        """Variance model to be fit.

        Arguments
        ---------
        var_pipe: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Pipeline variance.
        var_lss: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Large-scale structure variance.
        eta: float
            Pipeline variance calibration scalar.

        Returns
        -------
        :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Expected variance of deltas.
        """
        return eta * var_pipe + var_lss

    def __init__(
            self, w1obs, w2obs, nwbins,
            var1=1e-5, var2=2., nvarbins=100,
            nsubsamples=100, comm=None
    ):
        self.nwbins = nwbins
        self.nvarbins = nvarbins
        self.comm = comm

        # Set up wavelength and inverse variance bins
        wave_edges, self.dwobs = np.linspace(
            w1obs, w2obs, nwbins + 1, retstep=True)
        self.waveobs = (wave_edges[1:] + wave_edges[:-1]) / 2
        self.ivar_edges = np.logspace(
            -np.log10(var2), -np.log10(var1), nvarbins + 1)
        self.ivar_centers = (self.ivar_edges[1:] + self.ivar_edges[:-1]) / 2

        # Set up arrays to store statistics
        self.minlength = (self.nvarbins + 2) * (self.nwbins + 2)

        self.num_pixels = np.zeros(self.minlength, dtype=int)
        self.num_qso = np.zeros(self.minlength, dtype=int)

        # If ran with MPI, save mpi_rank first
        # Then shift each container to remove possibly over adding to 0th bin.
        if comm is not None:
            self.mpi_rank = comm.Get_rank()
        else:
            self.mpi_rank = 0

        self.var_delta_subs = SubsampleCov(
            self.minlength, nsubsamples, self.mpi_rank)
        self.mean_delta_subs = SubsampleCov(
            self.minlength, nsubsamples, self.mpi_rank)
        self.var2_delta_subs = SubsampleCov(
            self.minlength, nsubsamples, self.mpi_rank)

    def reset(self):
        """Reset delta and num arrays to zero."""
        self.var_delta_subs.reset(self.mpi_rank)
        self.mean_delta_subs.reset(self.mpi_rank)
        self.var2_delta_subs.reset(self.mpi_rank)
        self.num_pixels = 0
        self.num_qso = 0

    def add(self, wave, delta, ivar):
        """Add statistics of a single spectrum. Updates delta and num arrays.

        Assumes no spectra has `wave < w1obs` or `wave > w2obs`.

        Arguments
        ---------
        wave: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Wavelength array.
        delta: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Delta array.
        ivar: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Inverse variance array.
        """
        # add 1 to match searchsorted/bincount output/input
        wave_indx = ((wave - self.waveobs[0]) / self.dwobs + 1.5).astype(int)
        ivar_indx = np.searchsorted(self.ivar_edges, ivar)
        all_indx = ivar_indx + wave_indx * (self.nvarbins + 2)

        npix = np.bincount(all_indx, minlength=self.minlength)
        self.num_pixels += npix

        self.mean_delta_subs.add_measurement(
            np.bincount(all_indx, weights=delta, minlength=self.minlength),
            npix)
        self.var_delta_subs.add_measurement(
            np.bincount(all_indx, weights=delta**2, minlength=self.minlength),
            npix)
        self.var2_delta_subs.add_measurement(
            np.bincount(all_indx, weights=delta**4, minlength=self.minlength),
            npix)

        npix[npix > 0] = 1
        self.num_qso += npix

    def _allreduce(self):
        """Sums statistics from all MPI process, and calculates mean, variance
        and error on the variance.

        It also calculates the delete-one Jackknife variance of var_delta over
        ``nsubsamples``.
        """
        if self.comm is not None:
            self.mean_delta_subs.allreduce(self.comm, MPI.IN_PLACE)
            self.var_delta_subs.allreduce(self.comm, MPI.IN_PLACE)
            self.var2_delta_subs.allreduce(self.comm, MPI.IN_PLACE)

            self.comm.Allreduce(MPI.IN_PLACE, self.num_pixels)
            self.comm.Allreduce(MPI.IN_PLACE, self.num_qso)

        self.var_delta_subs.get_mean_n_var()
        self.mean_delta_subs.get_mean()
        self.var2_delta_subs.get_mean()

        self.var_delta_subs.mean -= self.mean_delta_subs.mean**2
        self.var2_delta_subs.mean -= self.var_delta_subs.mean**2

        w = self.num_pixels > 0
        self.var2_delta_subs.mean[w] /= self.num_pixels[w]

    def _smooth_fit_results(self, fit_results, std_results):
        w = fit_results > 0

        # Smooth new estimates
        if fit_results.ndim == 1:
            spl = UnivariateSpline(
                self.waveobs[w], fit_results[w], w=1 / std_results[w])

            nfails = np.sum(fit_results == 0)
            fit_results = spl(self.waveobs)
        # else ndim == 2
        else:
            w = w[:, 0]
            spl1 = UnivariateSpline(
                self.waveobs[w], fit_results[w, 0], w=1 / std_results[w, 0])
            spl2 = UnivariateSpline(
                self.waveobs[w], fit_results[w, 1], w=1 / std_results[w, 1])

            nfails = np.sum(fit_results[:, 0] == 0)
            fit_results[:, 0] = spl1(self.waveobs)
            fit_results[:, 1] = spl2(self.waveobs)

        return fit_results, nfails

    def get_var_delta_error(self, method="gauss"):
        """ Calculate the error (sigma) on var_delta using a given method.

        ``method="gauss"``::

        Observed var2_delta using delta**4 statistics are used as has been done
        before.

        ``method="regJack"`` and ``method="doubleJack"``::

        The variance on var_delta is first calculated by delete-one Jackknife
        over ``nsubsamples``. This is regularized by calculated var2_delta
        (Gaussian estimates), where if Jackknife variance is smaller than the
        Gaussian estimate, it is replaced by the Gaussian estimate.

        Arguments
        ---------
        method: str, default: gauss
            Method to estimate error on var_delta

        Returns
        ---------
        error_estimates: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Error (sigma) on var_delta
        """
        assert (
            method == "gauss" or method == "regJack" or method == "doubleJack")

        if method == "gauss":
            error_estimates = np.sqrt(self.var2_delta_subs.mean)

        else:
            # Regularized jackknife errors
            error_estimates = np.where(
                self.var_delta_subs.variance > self.var2_delta_subs.mean,
                self.var_delta_subs.variance,
                self.var2_delta_subs.mean
            )

            error_estimates = np.sqrt(error_estimates)

        return error_estimates

    def _fit_array_shape_assert(self, arr):
        assert (arr.ndim == 1 or arr.ndim == 2)
        assert (arr.shape[0] == self.nwbins)
        if arr.ndim == 2:
            assert (arr.shape[1] == 2)

    def fit(self, initial_guess, method="gauss"):
        """ Syncronize all MPI processes and fit for ``var_lss`` and ``eta``.

        Second axis always contains ``eta`` values. Example::

            var_lss = initial_guess[:, 0]
            eta = initial_guess[:, 1]

        This implemented using :func:`scipy.optimize.curve_fit` with
        ``sqrt(var2_delta_subs)`` as absolute errors. Domain is bounded to
        ``(0, 2)``. These fits are then smoothed via
        :external+scipy:py:class:`scipy.interpolate.UnivariateSpline`
        using weights from ``curve_fit``,
        while missing values or failed wavelength bins are extrapolated.

        Arguments
        ---------
        initial_guess: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Initial guess for var_lss and eta. If 1D array, eta is fixed to
            one. If 2D, its shape must be ``(nwbins, 2)``.
        method: str, default: gauss
            Error estimation method

        Returns
        ---------
        fit_results: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Smoothed fit results at observed wavelengths where missing values
            are extrapolated. 1D array containing LSS variance if
            ``initial_guess`` is 1D. 2D containing eta values on the second
            axis if ``initial_guess`` is 2D ndarray.
        std_results: :external+numpy:py:class:`ndarray <numpy.ndarray>`
            Error on ``var_lss`` from sqrt of ``curve_fit`` output. Same
            behavior as ``fit_results``.
        """
        self._fit_array_shape_assert(initial_guess)
        self._allreduce()

        error_estimates = self.get_var_delta_error(method)
        fit_results = np.zeros_like(initial_guess)
        std_results = np.zeros_like(initial_guess)

        for iwave in range(self.nwbins):
            i1 = (iwave + 1) * (self.nvarbins + 2)
            i2 = i1 + self.nvarbins
            wbinslice = np.s_[i1:i2]

            w = self.num_pixels[wbinslice] > VarLSSFitter.min_no_pix
            w &= self.num_qso[wbinslice] > VarLSSFitter.min_no_qso

            if w.sum() == 0:
                warn_mpi(
                    "Not enough statistics for VarLSSFitter at"
                    f" wave_obs: {self.waveobs[iwave]:.2f}.",
                    self.mpi_rank)
                continue

            try:
                pfit, pcov = curve_fit(
                    VarLSSFitter.variance_function,
                    1 / self.ivar_centers[w],
                    self.var_delta_subs.mean[wbinslice][w],
                    p0=initial_guess[iwave],
                    sigma=error_estimates[wbinslice][w],
                    absolute_sigma=True,
                    check_finite=True,
                    bounds=(0, 2)
                )
            except Exception as e:
                warn_mpi(
                    "VarLSSFitter failed at wave_obs: "
                    f"{self.waveobs[iwave]:.2f}. "
                    f"Reason: {e}. Extrapolating.",
                    self.mpi_rank)
            else:
                fit_results[iwave] = pfit
                std_results[iwave] = np.sqrt(np.diag(pcov))

        # Smooth new estimates
        fit_results, nfails = self._smooth_fit_results(
            fit_results, std_results)
        if nfails > 0:
            warn_mpi(
                f"VarLSSFitter failed and extrapolated at {nfails} points.",
                self.mpi_rank)

        return fit_results, std_results

    def save(self, fname, min_snr=0, max_snr=100):
        """Save variance statistics to FITS file.

        Arguments
        ---------
        fname: str
            Filename to be written. It is always overwritten.
        min_snr: float, default: 0
            Minimum SNR in this sample to be written into header.
        max_snr: float, default: 100
            Maximum SNR in this sampleto be written into header.

        Returns
        -------
        mpi_saver: MPISaver
            To save additional data or to close manually.
        """
        mpi_saver = MPISaver(fname, self.mpi_rank)

        hdr_dict = {
            'MINNPIX': VarLSSFitter.min_no_pix,
            'MINNQSO': VarLSSFitter.min_no_qso,
            'MINSNR': min_snr,
            'MAXSNR': max_snr,
            'WAVE1': self.wave1,
            'WAVE2': self.wave2,
            'NWBINS': self.nwbins,
            'VAR1': 1 / self.ivar_centers[-1],
            'VAR2': 1 / self.ivar_centers[0],
            'NVARBINS': self.ivar_centers.size
        }

        mpi_saver.write([
            np.repeat(self.waveobs, self.ivar_centers.size),
            np.tile(self.ivar_centers, self.nwbins),
            self.mean_delta_subs.mean, self.var_delta_subs.mean,
            self.var_delta_subs.variance, self.var2_delta_subs.mean,
            self.num_pixels, self.num_qso],
            names=['wave', 'ivar_pipe', 'mean_delta', 'var_delta',
                   'varjack_delta', 'var2_delta', 'num_pixels', 'num_qso'],
            extname="VAR_STATS", header=hdr_dict
        )

        return mpi_saver
