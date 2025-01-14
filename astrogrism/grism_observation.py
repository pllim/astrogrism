import asdf
from astropy.io import fits
from astropy.modeling import models
from astropy import units as u
from gwcs import wcs as gwcs
from gwcs import coordinate_frames as cf
import numpy as np
import pathlib

from astrogrism.HST.transform_models import (WFC3IRForwardGrismDispersion,
                                             WFC3IRBackwardGrismDispersion)
from astrogrism.HST.dispersion_models import DISPXY_Extension

pkg_dir = pathlib.Path(__file__).parent.absolute()


class GrismObs():
    """
    Base class for astrogrism package. Stores all necessary information about
    a single grism observation.
    """

    def __init__(self, grism_image, direct_image=None, telescope=None, instrument=None,
                 detector=None, filter=None, ccd=None):

        # Read grism image file if string input
        if isinstance(grism_image, str):
            self.grism_image = fits.open(grism_image)
        elif isinstance(grism_image, fits.HDUList):
            self.grism_image = grism_image
        else:
            raise TypeError("grism_image must be either a string filepath or FITS HDUList")

        # Read direct image file if string input
        if direct_image is None:
            self.direct_image = None
        if isinstance(direct_image, str):
            self.direct_image = fits.open(direct_image)
        elif isinstance(direct_image, fits.HDUList) or direct_image is None:
            self.direct_image = direct_image
        else:
            raise TypeError("direct_image must be either a string filepath or FITS HDUList")

        # Parse grism image file header for meta info
        self.grism_header = self.grism_image["PRIMARY"].header

        # Attempt to retrieve any information missing from the header (e.g. SIP)
        # Should probably make these properties instead.
        if telescope is None:
            self.telescope = self.grism_header["TELESCOP"]
        else:
            self.telescope = telescope

        if instrument is None:
            self.instrument = self.grism_header["INSTRUME"]
        else:
            self.instrument = instrument

        if filter is None:
            self.filter = self.grism_header["FILTER"]
        else:
            self.filter = filter

        # Build GWCS geometric transform pipeline
        print(self.instrument)
        if self.filter == "G280":
            # Need to build transforms for both channels of UVIS
            self.geometric_transforms = {}
            self.geometric_transforms["CCD1"] = self._build_geometric_transforms(channel=1)
            self.geometric_transforms["CCD2"] = self._build_geometric_transforms(channel=2)
        else:
            self.geometric_transforms = self._build_geometric_transforms()

    def _build_geometric_transforms(self, channel=None):

        """
        Build transform pipeline under the hood so the user doesn't need
        to worry about it.

        TODO:
        - Try to get SIP coefficients from grism observation header before
        resorting to premade file. _flt files do not have inverse SIP
        coefficients, so that would require calculating them on the fly.
        """

        # Register custom asdf extension
        asdf.get_config().add_extension(DISPXY_Extension())

        # Get paths to premade configuration files
        config_dir = pkg_dir / 'config' / self.telescope

        # Account for additional specifications needed for WFC3 instrument and filter
        if self.telescope == "HST":
            if self.filter in ("G102", "G141"):
                instrument = self.instrument + "_IR"
                filter = self.filter
            elif self.filter == "G280":
                instrument = f"{self.instrument}_UVIS"
                filter = f"{self.filter}_CCD{channel}"
        else:
            instrument = self.instrument
            filter = self.filter

        sip_file = config_dir / "{}_distortion.fits".format(instrument)
        spec_wcs_file = config_dir / "{}_{}_specwcs.asdf".format(
                                                       self.instrument,
                                                       filter)

        # Build the grism_detector <-> detector transforms
        with asdf.open(str(spec_wcs_file)) as f:
            specwcs = f.tree
        displ = specwcs['displ']
        dispx = specwcs['dispx']
        dispy = specwcs['dispy']
        try:
            invdispl = specwcs['invdispl']
        except KeyError:
            invdispl = None
        invdispx = specwcs['invdispx']
        orders = specwcs['order']

        gdetector = cf.Frame2D(name='grism_detector',
                               axes_order=(0, 1),
                               unit=(u.pix, u.pix))
        det2det = WFC3IRForwardGrismDispersion(orders,
                                               lmodels=displ,
                                               xmodels=invdispx,
                                               ymodels=dispy)
        # TODO: Decide where to raise a warning if we can't do the backward
        # grism transformation (UVIS, at least for now).
        if invdispl is not None:
            det2det.inverse = WFC3IRBackwardGrismDispersion(orders,
                                                            lmodels=invdispl,
                                                            xmodels=dispx,
                                                            ymodels=dispy)
        else:
            det2det.inverse = WFC3IRBackwardGrismDispersion(orders,
                                                            lmodels=displ,
                                                            xmodels=dispx,
                                                            ymodels=dispy,
                                                            interpolate_t=True)

        grism_pipeline = [(gdetector, det2det)]

        # Now add the detector -> world transform
        sip_hdus = fits.open(str(sip_file))

        # Get the correct hdu from the SIP file
        if channel is not None:
            for hdu in sip_hdus:
                if "CCDCHIP" in hdu.header and hdu.header["CCDCHIP"] == channel:
                    sip_hdu = hdu
        else:
            sip_hdu = sip_hdus[1]

        acoef = dict(sip_hdu.header['A_*'])
        a_order = acoef.pop('A_ORDER')
        bcoef = dict(sip_hdu.header['B_*'])
        b_order = bcoef.pop('B_ORDER')

        # Get the inverse SIP polynomial coefficients from file
        apcoef = dict(sip_hdu.header['AP_*'])
        bpcoef = dict(sip_hdu.header['BP_*'])

        sip_hdus.close()

        try:
            ap_order = apcoef.pop('AP_ORDER')
            bp_order = bpcoef.pop('BP_ORDER')
        except ValueError:
            raise

        crpix = [self.grism_image[1].header['CRPIX1'], self.grism_image[1].header['CRPIX2']]

        crval = [self.grism_image[1].header['CRVAL1'],
                 self.grism_image[1].header['CRVAL2']]

        cdmat = np.array([[self.grism_image[1].header['CD1_1'],
                           self.grism_image[1].header['CD1_2']],
                          [self.grism_image[1].header['CD2_1'],
                           self.grism_image[1].header['CD2_2']]])

        a_polycoef = {}
        for key in acoef:
            a_polycoef['c' + key.split('A_')[1]] = acoef[key]

        b_polycoef = {}
        for key in bcoef:
            b_polycoef['c' + key.split('B_')[1]] = bcoef[key]

        ap_polycoef = {}
        for key in apcoef:
            ap_polycoef['c' + key.split('AP_')[1]] = apcoef[key]

        bp_polycoef = {}
        for key in bpcoef:
            bp_polycoef['c' + key.split('BP_')[1]] = bpcoef[key]

        a_poly = models.Polynomial2D(a_order, **a_polycoef)
        b_poly = models.Polynomial2D(b_order, **b_polycoef)
        ap_poly = models.Polynomial2D(ap_order, **ap_polycoef)
        bp_poly = models.Polynomial2D(bp_order, **bp_polycoef)

        # See SIP definition paper for definition of u, v, f, g
        # TODO: maybe don't hardcode the pole to rotate around (180 for HST)?
        SIP_forward = (models.Shift(-(crpix[0]-1)) & models.Shift(-(crpix[1]-1)) |
                       models.Mapping((0, 1, 0, 1, 0, 1)) | a_poly & b_poly & models.Identity(2) |
                       models.Mapping((0, 2, 1, 3)) |
                       models.math.AddUfunc() & models.math.AddUfunc() |
                       models.AffineTransformation2D(matrix=cdmat) | models.Pix2Sky_TAN() |
                       models.RotateNative2Celestial(crval[0], crval[1], 180))

        SIP_backward = (models.RotateCelestial2Native(crval[0], crval[1], 180) |
                        models.Sky2Pix_TAN() |
                        models.AffineTransformation2D(matrix=cdmat).inverse |
                        models.Mapping((0, 1, 0, 1, 0, 1)) |
                        ap_poly & bp_poly & models.Identity(2) |
                        models.Mapping((0, 2, 1, 3)) |
                        models.math.AddUfunc() & models.math.AddUfunc() |
                        models.Shift((crpix[0]-1)) & models.Shift((crpix[1]-1)))

        full_distortion_model = SIP_forward & models.Identity(2)
        full_distortion_model.inverse = SIP_backward & models.Identity(2)

        imagepipe = []

        det_frame = cf.Frame2D(name="detector")
        imagepipe.append((det_frame, full_distortion_model))

        world_frame = cf.CelestialFrame(name="world", unit=(u.Unit("deg"), u.Unit("deg")),
                                        axes_names=('lon', 'lat'), axes_order=(0, 1),
                                        reference_frame="ICRS")
        imagepipe.append((world_frame, None))

        grism_pipeline.extend(imagepipe)

        return gwcs.WCS(grism_pipeline)
