import os
import cv2
import numpy as np

from merlin.core import analysistask
from merlin.util import deconvolve
from merlin.util import aberration
from merlin.data import codebook


class Preprocess(analysistask.ParallelAnalysisTask):

    """
    An abstract class for preparing data for barcode calling. 
    """

    def _image_name(self, fov):
        destPath = self.dataSet.get_analysis_subdirectory(
                self.analysisName, subdirectory='preprocessed_images')
        return os.sep.join([destPath, 'fov_' + str(fov) + '.tif'])
    
    def get_pixel_histogram(self, fov=None):
        if fov is not None:
            return self.dataSet.load_numpy_analysis_result(
                'pixel_histogram', self.analysisName, fov, 'histograms')
        
        pixelHistogram = np.zeros(self.get_pixel_histogram(
                self.dataSet.get_fovs()[0]).shape)
        for f in self.dataSet.get_fovs():
            pixelHistogram += self.get_pixel_histogram(f)

        return pixelHistogram

    def _save_pixel_histogram(self, histogram, fov):
        self.dataSet.save_numpy_analysis_result(
            histogram, 'pixel_histogram', self.analysisName, fov, 'histograms')


class DeconvolutionPreprocess(Preprocess):

    def __init__(self, dataSet, parameters=None, analysisName=None):
        super().__init__(dataSet, parameters, analysisName)

        if 'highpass_sigma' not in self.parameters:
            self.parameters['highpass_sigma'] = 3
        if 'decon_sigma' not in self.parameters:
            self.parameters['decon_sigma'] = 2
        if 'decon_filter_size' not in self.parameters:
            self.parameters['decon_filter_size'] = \
                int(2 * np.ceil(2 * self.parameters['decon_sigma']) + 1)
        if 'decon_iterations' not in self.parameters:
            self.parameters['decon_iterations'] = 20
        if 'codebook_index' not in self.parameters:
            self.parameters['codebook_index'] = 0

        self._highPassSigma = self.parameters['highpass_sigma']
        self._deconSigma = self.parameters['decon_sigma']
        self._deconIterations = self.parameters['decon_iterations']

        self.warpTask = self.dataSet.load_analysis_task(
            self.parameters['warp_task'])

    def fragment_count(self):
        return len(self.dataSet.get_fovs())
    
    def get_estimated_memory(self):
        return 2048

    def get_estimated_time(self):
        return 5

    def get_dependencies(self):
        return [self.parameters['warp_task']]

    def get_codebook(self) -> codebook.Codebook:
        return self.dataSet.get_codebook(self.parameters['codebook_index'])

    def write_processed_stack(self, fov, chromaticCorrector = None):
        import zstandard as zstd
        codebooks = self.dataSet.codebooks
        allMultiplexChannels = []
        for codebook in codebooks:
            allMultiplexChannels.extend(
                [self.dataSet.get_data_organization().get_data_channel_for_bit(b)
                 for b in codebook.get_bit_names()])
        seqChannels, seqNames = self.dataSet.get_data_organization().get_sequential_rounds()
        dataChannels = allMultiplexChannels + seqChannels
        zPositions = range(len(self.dataSet.get_z_positions()))
        imageDescription = self.dataSet.analysis_tiff_description(
                len(zPositions), len(dataChannels))
        imgName = self.dataSet._analysis_image_name(self, 'processed_images', fov)
        compressedImgName = imgName + '.zstd'
        with self.dataSet.writer_for_analysis_images(
                self, 'processed_images', fov) as outputTif:
            for ch in dataChannels:
                for z in zPositions:
                    if self.dataSet.get_data_organization().get_data_channel_name(ch) in ['polyT', 'DAPI']:
                        transformedImage = self.warpTask.get_aligned_image(fov, ch, z)
                        outputTif.save(transformedImage,
                                       photometric='MINISBLACK',
                                       metadata=imageDescription)
                    else:
                        processedImage = self.get_processed_image(fov, ch, z, chromaticCorrector)
                        outputTif.save(processedImage,
                                       photometric='MINISBLACK',
                                       metadata=imageDescription)

        cctx = zstd.ZstdCompressor()
        with open(imgName, 'rb') as ifh, open(compressedImgName, 'wb') as ofh:
            cctx.copy_stream(ifh, ofh)
        os.remove(imgName)

    def get_processed_image_set(
            self, fov, zIndex: int = None,
            chromaticCorrector: aberration.ChromaticCorrector = None
    ) -> np.ndarray:
        if zIndex is None:
            return np.array([[self.get_processed_image(
                fov, self.dataSet.get_data_organization()
                    .get_data_channel_for_bit(b), zIndex, chromaticCorrector)
                for zIndex in range(len(self.dataSet.get_z_positions()))]
                for b in self.get_codebook().get_bit_names()])
        else:
            return np.array([self.get_processed_image(
                fov, self.dataSet.get_data_organization()
                    .get_data_channel_for_bit(b), zIndex, chromaticCorrector)
                    for b in self.get_codebook().get_bit_names()])

    def get_processed_image(
            self, fov: int, dataChannel: int, zIndex: int,
            chromaticCorrector: aberration.ChromaticCorrector = None
    ) -> np.ndarray:
        inputImage = self.warpTask.get_aligned_image(fov, dataChannel, zIndex,
                                                     chromaticCorrector)
        return self._preprocess_image(inputImage)

    def _run_analysis(self, fragmentIndex):
        warpTask = self.dataSet.load_analysis_task(
                self.parameters['warp_task'])

        histogramBins = np.arange(0, np.iinfo(np.uint16).max, 1)
        pixelHistogram = np.zeros(
                (self.get_codebook().get_bit_count(), len(histogramBins)-1))

        # this currently only is to calculate the pixel histograms in order
        # to estimate the initial scale factors. This is likely unnecessary
        for bi, b in enumerate(self.get_codebook().get_bit_names()):
            dataChannel = self.dataSet.get_data_organization()\
                    .get_data_channel_for_bit(b)
            for i in range(len(self.dataSet.get_z_positions())):
                inputImage = warpTask.get_aligned_image(
                        fragmentIndex, dataChannel, i)
                deconvolvedImage = self._preprocess_image(inputImage)

                pixelHistogram[bi, :] += np.histogram(
                        deconvolvedImage, bins=histogramBins)[0]

        self._save_pixel_histogram(pixelHistogram, fragmentIndex)

    def _preprocess_image(self, inputImage: np.ndarray) -> np.ndarray:
        highPassFilterSize = int(2 * np.ceil(2 * self._highPassSigma) + 1)
        deconFilterSize = self.parameters['decon_filter_size']

        filteredImage = inputImage.astype(float) - cv2.GaussianBlur(
            inputImage, (highPassFilterSize, highPassFilterSize),
            self._highPassSigma, borderType=cv2.BORDER_REPLICATE)
        filteredImage[filteredImage < 0] = 0
        deconvolvedImage = deconvolve.deconvolve_lucyrichardson(
            filteredImage, deconFilterSize, self._deconSigma,
            self._deconIterations).astype(np.uint16)
        return deconvolvedImage
