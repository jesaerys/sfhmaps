import sys
import numpy as np
import matplotlib.pyplot as pl

import astropy.constants as constants
import fsps

from sfhutils import weights_1DLinear, load_angst_sfh
import attenuation

lsun = constants.L_sun.cgs.value
pc = constants.pc.cgs.value
lightspeed = 2.998e18 #AA/s
#value to go from L_sun/AA to erg/s/cm^2/AA at 10pc
to_cgs = lsun/(4.0 * np.pi * (pc*10)**2 )

def gauss(x, mu, A, sigma):
    """
    Project a sequence of gaussians onto the x vector, using broadcasting.
    """
    mu, A, sigma = np.atleast_2d(mu), np.atleast_2d(A), np.atleast_2d(sigma)
    val = A/(sigma * np.sqrt(np.pi * 2)) * np.exp(-(x[:,None] - mu)**2/(2 * sigma**2))
    return val.sum(axis = -1)

def convert_burst_pars(fwhm_burst = 0.05, f_burst = 0.5, contrast = 5,
                       bin_width = 1.0, bin_sfr = 1e9):

    """
    Perform the conversion from a burst fraction, width, and
    'contrast' to to a set of gaussian bursts stochastically
    distributed in time, each characterized by a burst time, a width,
    and an amplitude.  Also returns the SFR in the non-bursting mode.
    """
    
    #print(bin_width, bin_sfr)
    width, mstar = bin_width, bin_width * bin_sfr
    if width < fwhm_burst * 2:
        f_burst = 0.0 #no bursts if bin is short - they are resolved
    #constant SF component
    a = mstar * (1 - f_burst) /width
    #determine burst_parameters
    sigma = fwhm_burst / 2.355
    maxsfr = contrast * a
    A = maxsfr * (sigma * np.sqrt(np.pi * 2))
    if A > 0:
        nburst = np.round(mstar * f_burst / A)
        #recalculate A to preserve total mass formed in the face of burst number quntization
        if nburst > 0:
            A = mstar * f_burst / nburst
        else:
            A = 0
            a = mstar/width
    else:
        nburst = 0
        a = mstar/width
        
    tburst = np.random.uniform(0,width, nburst)
    #print(a, nburst, A, sigma)
    return [a, tburst, A, sigma]

def burst_sfh(fwhm_burst = 0.05, f_burst = 0.5, contrast = 5,
              sfh = None, bin_res = 10.):
    """
    Given a binned SFH as a numpy structured array, and burst
    parameters, generate a realization of the SFH at high temporal
    resolution.
    """
    #
    a, tburst, A, sigma, f_burst_actual = [],[],[],[],[]
    for i,abin in enumerate(sfh):
     #   print('------\nbin #{0}'.format(i))
        res = convert_burst_pars(fwhm_burst = fwhm_burst, f_burst = f_burst, contrast = contrast,
                             bin_width = (abin['t2']-abin['t1']), bin_sfr = abin['sfr'])
        a += [res[0]]
        if len(res[1]) > 0:
            tburst += (res[1] + abin['t1']).tolist()
            A += len(res[1]) * [res[2]]
            sigma += len(res[1]) * [res[3]]
        #f_burst_actual += [res[4]]
    if len(sigma) == 0:
        #if there were no bursts, set the time resolution to be 1/bin_res of the
        # shortest bin width.
        dt = (sfh['t2'] - sfh['t1']).min()/(1.0 * bin_res)
    else:
        dt = np.min(sigma)/5. #make sure you sample the bursts reasonably well
    times = np.arange(np.round(sfh['t2'].max()/dt)) * dt
    #print(dt, np.round(sfh['t2'].max()/dt))
    #sys.exit()
    #figure out which bin each time is in
    bins = [sfh[0]['t1']] + sfh['t2'].tolist()
    bin_num = np.digitize(times, bins) -1
    #calculate SFR from all components
    sfr = np.array(a)[bin_num] + gauss(times, tburst, A, sigma)
    
    return times, sfr, f_burst_actual


def bursty_sps(lookback_time, lt, sfr, sps,
               av = None, dav = None, dust_curve = attenuation.cardelli):
    """
    Obtain the spectrum of a stellar poluation with arbitrary complex
    SFH at a given lookback time.  The SFH is provided in terms of SFR
    vs t_lookback. Note that this in in contrast to the normal
    specification in terms of time since the big bang. Interpolation
    of the available SSPs to the time sequence of the SFH is
    accomplished by linear interpolation in log t.  Highly oscillatory
    SFHs require dense sampling of the temporal axis to obtain
    accurate results.

    :param lookback_time: scalar or ndarray, shape (ntarg)
        The lookback time(s) at which to obtain the spectrum.
        
    :param lt: ndarray, shape (ntime)
        The lookback time sequence of the provided SFH.  Assumed to
        have have equal linear time intervals, i.e. to be a regular
        grid in logt
        
    :param sfr: ndarray, shape (ntime)
        The SFR corresponding to each element of lt
        
    :param sps: fsps.StellarPopulation instance
        The fsps stellar population (with metallicty and IMF
        parameters set) to use for the SSP spectra.

    :returns wave: ndarray, shape (nwave)
        The wavelength array
        
    :returns int_spec: ndarray, shape(nwave)
        The integrated spectrum at lookback_time
        
    :returns aw: ndarray, shape(ntarg, nage)
        The total weights of each SSP spectrum for each requested
        lookback_time.  Useful for debugging.
    """
    
    dt = lt[1] - lt[0]
    sps.params['sfh'] = 0 #set to SSPs
    #get *all* the ssps
    wave, spec = sps.get_spectrum(peraa = True, tage = 0)
    spec, lir = redden(wave, spec, av = av, dav = dav, dust_curve = dust_curve)
    ssp_ages = 10**sps.log_age #in yrs
    target_lt = np.atleast_1d(lookback_time)
    #set up output
    int_spec = np.zeros( [ len(target_lt), len(wave) ] )
    aw = np.zeros( [ len(target_lt), len(ssp_ages) ] )

    for i,tl in enumerate(target_lt):
        valid = (lt >= tl)
        inds, weights = weights_1DLinear(np.log(ssp_ages), np.log(lt[valid] - tl))
        #aggregate the weights for each index, after accounting for SFR
        agg_weights = np.bincount( inds.flatten(),
                                   weights = (weights * sfr[valid,None]).flatten(),
                                   minlength = len(ssp_ages) ) * dt
        int_spec[i,:] = (spec * agg_weights[:,None]).sum(axis = 0)
        aw[i,:] = agg_weights
    if lir is not None:
        lir_tot = (aw * lir[None,:]).sum(axis = -1)
        return wave, int_spec, aw, lir_tot
    else:
        return wave, int_spec, aw


def redden_analytic(wave, spec, av = None, dav = None,
                    dust_curve = None, wlo = 1216., whi = 2e4, **kwargs):
    k = dust_curve(wave)
    alambda = av / (np.log10(av+dav)) * ( 10**(-0.4 * k * (av+dav)) - 10**(-0.4 * k * av))
    spec_red = spec * alambda
    return spec_red, None
        
def redden_pieces(wave, spec, av = None, dav = None, nsplit = 9,
           dust_curve = None, wlo = 1216., whi = 2e4, **kwargs):
    
    """
    Redden the spectral components.  The attenuation of a given
    star is given by the model av + U(0,dav) where U is the uniform
    random function.  Extensive use of broadcasting.

    :params wave:  ndarray of shape (nwave)
        The wavelength vector.
    
    :params spec: ndarray of shape (nspec, nwave)
        The input spectra to redden. nspec is the number of spectra.
        
    :params av: scalar or ndarray of shape (nspec)
        The attenuation at V band, in magnitudes, that affects all
        stars equally.  Can be a scalar if its the same for all
        spectra or an ndarray to apply different reddenings to each
        spectrum.

    :params dav: scalar or ndarray of shape (nspec)
        The maximum differential attenuation, in V band magnitudes.
        Can be a scalar if it's the same for all spectra or an array
        to have a different value for each spectrum.  Stars are
        assumed to be affected by an random additional amount of
        attenuation uniformly distributed from 0 to dav.

    :params nsplit: (default 10.0)
        The number of pieces in which to split each spectrum when
        performing the integration over differntial attenuation.
        Higher nsplit ensures greater accuracy, especially for very
        large dav.  However, because of the broadcasting, large nsplit
        can result in memory constraint issues.

    :params dust_curve: function
        The function giving the attenuation curve normalized to the V
        band, \tau_lambda/\tau_v.  This function must accept a
        wavelength vector as its argument and return tau_lambda/tau_v
        at each wavelength.

    :returns spec: ndarray of shape (nwave, nspec)
        The attenuated spectra.

    :returns lir: ndarray of shape (nspec)
        The integrated difference between the unattenuated and
        attenuated spectra, for each spectrum. The integral is
        performed over the interval [wlo,whi].
        
    """

    if (av is None) and (dav is None):
        return spec, None
    if dust_curve is None:
        print('Warning:  no dust curve was given')
        return spec, None
    #only split if there's a nonzero dAv 
    nsplit = nsplit * np.any(dav > 0) + 1
    lisplit = spec/nsplit
    #enable broadcasting if av and dav aren't vectors
    #  and convert to an optical depth instead of an attenuation
    av = np.atleast_1d(av)/1.086
    dav = np.atleast_1d(dav)/1.086
    lisplit = np.atleast_2d(lisplit)
    #uniform distribution from Av to Av + dAv
    avdist = av[None, :] + dav[None,:] * ((np.arange(nsplit) + 0.5)/nsplit)[:,None]
    #apply it
    print(avdist.shape)
    ee = (np.exp(-dust_curve(wave)[None,None,:] * avdist[:,:,None]))
    print(avdist.shape, ee.shape, lisplit.shape)
    spec_red = (ee * lisplit[None,:,:]).sum(axis = 0)
    #get the integral of the attenuated light in the optical-
    # NIR region of the spectrum
    opt = (wave >= wlo) & (wave <= whi) 
    lir = np.trapz((spec - spec_red)[:,opt], wave[opt], axis = -1)
    return np.squeeze(spec_red), lir
    
def examples(filename = '/Users/bjohnson/Projects/angst/sfhs/angst_sfhs/gr8.lowres.ben.v1.sfh',
             lookback_time = [1e9, 10e9]):
    """
    A quick test and demonstration of the algorithms.
    """
    sps = fsps.StellarPopulation()

    f_burst, fwhm_burst, contrast = 0.5, 0.05 * 1e9, 5
    sfh = load_angst_sfh(filename)
    sfh['t1'] = 10.**sfh['t1']
    sfh['t2'] = 10.**sfh['t2']
    sfh['sfr'][0] *=  1 - (sfh['t1'][0]/sfh['t2'][0])
    sfh[0]['t1'] = 0.
    mtot = ((sfh['t2'] - sfh['t1']) * sfh['sfr']).sum()
    lt, sfr, fb = burst_sfh(fwhm_burst = fwhm_burst, f_burst = f_burst, contrast = contrast, sfh = sfh)

    wave, spec, aw = bursty_sps(lookback_time, lt, sfr, sps)

    pl.figure()
    for i,t in enumerate(lookback_time):
        pl.plot(wave, spec[i,:], label = r'$t_{{lookback}} = ${0:5.1f} Gyr'.format(t/1e9))
    pl.legend()
    pl.xlim(1e3,1e4)
    pl.xlabel('wave')
    pl.ylabel(r'$F_\lambda$')

    fig, ax = pl.subplots(2,1)
    for i,t in enumerate(lookback_time):
        ax[1].plot(10**sps.log_age, aw[i,:], label = r'$t_{{lookback}} = ${0:5.1f} Gyr'.format(t/1e9), marker ='o', markersize = 2)
        print(aw[i,:].sum(), mtot, aw[i,:].sum()/mtot)
    ax[1].set_xlabel('SSP age - lookback time')
    ax[1].set_ylabel('Mass')
    ax[1].legend(loc = 'upper left')

    ax[0].plot(lt, sfr, 'k')
    ax[0].set_xlabel('lookback time')
    ax[0].set_ylabel('SFR')
    ax[0].set_title(r'f$_{{burst}}={0:3.1f}$, fwhm$_{{burst}}=${1:3.0f}Myr, contrast ={2}'.format(f_burst, fwhm_burst/1e6, contrast))
    for t in lookback_time:
        ax[0].axvline(x = t, color = 'r', linestyle =':', linewidth = 5)
    pl.show()