"""This module holds classes for image loading and manipulation."""
from __future__ import annotations

import copy
import io
import json
import math
import os
import os.path as osp
import re
from collections import Counter
from datetime import datetime
from io import BufferedReader, BytesIO
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Sequence, Union

import argue
import matplotlib.pyplot as plt
import numpy as np
import pydicom
import scipy.ndimage.filters as spf
from PIL import Image as pImage
from PIL.PngImagePlugin import PngInfo
from pydicom.errors import InvalidDicomError
from scipy import ndimage
from skimage.draw import disk

from ..settings import PATH_TRUNCATION_LENGTH, get_dicom_cmap
from .geometry import Point
from .io import (
    TemporaryZipDirectory,
    get_url,
    is_dicom_image,
    retrieve_dicom_file,
    retrieve_filenames,
)
from .profile import stretch as stretcharray
from .utilities import decode_binary, is_close

ARRAY = "Array"
DICOM = "DICOM"
IMAGE = "Image"

FILE_TYPE = "file"
STREAM_TYPE = "stream"

XIM_PROP_INT = 0
XIM_PROP_DOUBLE = 1
XIM_PROP_STRING = 2
XIM_PROP_DOUBLE_ARRAY = 4
XIM_PROP_INT_ARRAY = 5

MM_PER_INCH = 25.4

ImageLike = Union["DicomImage", "ArrayImage", "FileImage", "LinacDicomImage"]


def equate_images(image1: ImageLike, image2: ImageLike) -> tuple[ImageLike, ImageLike]:
    """Crop and resize two images to make them:
      * The same pixel dimensions
      * The same DPI

    The usefulness of the function comes when trying to compare images from different sources.
    The best example is calculating gamma on a machine log fluence and EPID image. The physical
    and pixel dimensions must be normalized, the SID normalized

    Parameters
    ----------
    image1 : {:class:`~pylinac.core.image.ArrayImage`, :class:`~pylinac.core.image.DicomImage`, :class:`~pylinac.core.image.FileImage`}
        Must have DPI and SID.
    image2 : {:class:`~pylinac.core.image.ArrayImage`, :class:`~pylinac.core.image.DicomImage`, :class:`~pylinac.core.image.FileImage`}
        Must have DPI and SID.

    Returns
    -------
    image1 : :class:`~pylinac.core.image.ArrayImage`
    image2 : :class:`~pylinac.core.image.ArrayImage`
        The returns are new instances of Images.
    """
    image1 = copy.deepcopy(image1)
    image2 = copy.deepcopy(image2)
    # crop images to be the same physical size
    # ...crop height
    physical_height_diff = image1.physical_shape[0] - image2.physical_shape[0]
    if physical_height_diff < 0:  # image2 is bigger
        img = image2
    else:
        img = image1
    pixel_height_diff = abs(int(round(-physical_height_diff * img.dpmm / 2)))
    img.crop(pixel_height_diff, edges=("top", "bottom"))

    # ...crop width
    physical_width_diff = image1.physical_shape[1] - image2.physical_shape[1]
    if physical_width_diff > 0:
        img = image1
    else:
        img = image2
    pixel_width_diff = abs(int(round(physical_width_diff * img.dpmm / 2)))
    img.crop(pixel_width_diff, edges=("left", "right"))

    # resize images to be of the same shape
    zoom_factor = image1.shape[1] / image2.shape[1]
    image2_array = ndimage.interpolation.zoom(image2.as_type(float), zoom_factor)
    image2 = load(image2_array, dpi=image2.dpi * zoom_factor)

    return image1, image2


def is_image(path: str | io.BytesIO | ImageLike | np.ndarray) -> bool:
    """Determine whether the path is a valid image file.

    Returns
    -------
    bool
    """
    return any((_is_array(path), _is_dicom(path), _is_image_file(path)))


def retrieve_image_files(path: str) -> list[str]:
    """Retrieve the file names of all the valid image files in the path.

    Returns
    -------
    list
        Contains strings pointing to valid image paths.
    """
    return retrieve_filenames(directory=path, func=is_image)


def load(path: str | Path | ImageLike | np.ndarray | BinaryIO, **kwargs) -> ImageLike:
    r"""Load a DICOM image, JPG/TIF/BMP image, or numpy 2D array.

    Parameters
    ----------
    path : str, file-object
        The path to the image file or data stream or array.
    kwargs
        See :class:`~pylinac.core.image.FileImage`, :class:`~pylinac.core.image.DicomImage`,
        or :class:`~pylinac.core.image.ArrayImage` for keyword arguments.

    Returns
    -------
    ::class:`~pylinac.core.image.FileImage`, :class:`~pylinac.core.image.ArrayImage`, or :class:`~pylinac.core.image.DicomImage`
        Return type depends on input image.

    Examples
    --------
    Load an image from a file and then apply a filter::

        >>> from pylinac.core.image import load
        >>> my_image = r"C:\QA\image.tif"
        >>> img = load(my_image)  # returns a FileImage
        >>> img.filter(5)

    Loading from an array is just like loading from a file::

        >>> arr = np.arange(36).reshape(6, 6)
        >>> img = load(arr)  # returns an ArrayImage
    """
    if isinstance(path, BaseImage):
        return path

    if _is_array(path):
        return ArrayImage(path, **kwargs)
    elif _is_dicom(path):
        return DicomImage(path, **kwargs)
    elif _is_image_file(path):
        return FileImage(path, **kwargs)
    else:
        raise TypeError(
            f"The argument `{path}` was not found to be a valid DICOM file, Image file, or array"
        )


def load_url(url: str, progress_bar: bool = True, **kwargs) -> ImageLike:
    """Load an image from a URL.

    Parameters
    ----------
    url : str
        A string pointing to a valid URL that points to a file.

        .. note:: For some images (e.g. Github), the raw binary URL must be used, not simply the basic link.

    progress_bar: bool
        Whether to display a progress bar of download status.
    """
    filename = get_url(url, progress_bar=progress_bar)
    return load(filename, **kwargs)


def load_multiples(
    image_file_list: Sequence,
    method: str = "mean",
    stretch_each: bool = True,
    **kwargs,
) -> ImageLike:
    """Combine multiple image files into one superimposed image.

    Parameters
    ----------
    image_file_list : list
        A list of the files to be superimposed.
    method : {'mean', 'max', 'sum'}
        A string specifying how the image values should be combined.
    stretch_each : bool
        Whether to normalize the images being combined by stretching their high/low values to the same values across images.
    kwargs :
        Further keyword arguments are passed to the load function and stretch function.

    Examples
    --------
    Load multiple images::

        >>> from pylinac.core.image import load_multiples
        >>> paths = ['starshot1.tif', 'starshot2.tif']
        >>> superimposed_img = load_multiples(paths)
    """
    # load images
    img_list = [load(path, **kwargs) for path in image_file_list]
    first_img = img_list[0]

    # check that all images are the same size and stretch if need be
    for img in img_list:
        if img.shape != first_img.shape:
            raise ValueError("Images were not the same shape")
        if stretch_each:
            img.array = stretcharray(img.array, fill_dtype=kwargs.get("dtype"))

    # stack and combine arrays
    new_array = np.dstack(tuple(img.array for img in img_list))
    if method == "mean":
        combined_arr = np.mean(new_array, axis=2)
    elif method == "max":
        combined_arr = np.max(new_array, axis=2)
    elif method == "sum":
        combined_arr = np.sum(new_array, axis=2)

    # replace array of first object and return
    first_img.array = combined_arr
    return first_img


def _is_dicom(path: str | Path | io.BytesIO | ImageLike | np.ndarray) -> bool:
    """Whether the file is a readable DICOM file via pydicom."""
    return is_dicom_image(file=path)


def _is_image_file(path: str | Path) -> bool:
    """Whether the file is a readable image file via Pillow."""
    try:
        pImage.open(path)
        return True
    except:
        return False


def _is_array(obj: Any) -> bool:
    """Whether the object is a numpy array."""
    return isinstance(obj, np.ndarray)


class BaseImage:
    """Base class for the Image classes.

    Attributes
    ----------
    path : str
        The path to the image file.
    array : numpy.ndarray
        The actual image pixel array.
    """

    array: np.ndarray
    path: str | Path

    def __init__(
        self, path: str | Path | BytesIO | ImageLike | np.ndarray | BufferedReader
    ):
        """
        Parameters
        ----------
        path : str
            The path to the image.
        """
        source: FILE_TYPE | STREAM_TYPE

        if isinstance(path, (str, Path)) and not osp.isfile(path):
            raise FileExistsError(
                f"File `{path}` does not exist. Verify the file path name."
            )
        elif isinstance(path, (str, Path)) and osp.isfile(path):
            self.path = path
            self.base_path = osp.basename(path)
            self.source = FILE_TYPE
        else:
            self.source = STREAM_TYPE
            path.seek(0)
            try:
                self.path = str(Path(path.name))
            except AttributeError:
                self.path = ""

    @property
    def truncated_path(
        self,
    ) -> str:  # TODO: Use textwrap or pull out into util function
        if self.source == FILE_TYPE:
            path = str(self.path)
            if len(path) > PATH_TRUNCATION_LENGTH:
                return (
                    path[: PATH_TRUNCATION_LENGTH // 2]
                    + "..."
                    + path[-PATH_TRUNCATION_LENGTH // 2 :]
                )
            else:
                return path
        else:
            return ""  # was from stream, no path

    @classmethod
    def from_multiples(
        cls,
        filelist: list[str],
        method: str = "mean",
        stretch: bool = True,
        **kwargs,
    ) -> ImageLike:
        """Load an instance from multiple image items. See :func:`~pylinac.core.image.load_multiples`."""
        return load_multiples(filelist, method, stretch, **kwargs)

    @property
    def center(self) -> Point:
        """Return the center position of the image array as a Point.
        Even-length arrays will return the midpoint between central two indices. Odd will return the central index."""
        x_center = (self.shape[1] / 2) - 0.5
        y_center = (self.shape[0] / 2) - 0.5
        return Point(x_center, y_center)

    @property
    def physical_shape(self) -> (float, float):
        """The physical size of the image in mm."""
        return self.shape[0] / self.dpmm, self.shape[1] / self.dpmm

    def date_created(self, format: str = "%A, %B %d, %Y") -> str:
        """The date the file was created. Tries DICOM data before falling back on OS timestamp"""
        date = None
        try:
            date = datetime.strptime(
                self.metadata.InstanceCreationDate
                + str(round(float(self.metadata.InstanceCreationTime))),
                "%Y%m%d%H%M%S",
            )
            date = date.strftime(format)
        except (AttributeError, ValueError):
            try:
                date = datetime.strptime(self.metadata.StudyDate, "%Y%m%d")
                date = date.strftime(format)
            except:
                pass
        if date is None:
            try:
                date = datetime.fromtimestamp(osp.getctime(self.path)).strftime(format)
            except AttributeError:
                date = "Unknown"
        return date

    def plot(
        self, ax: plt.Axes = None, show: bool = True, clear_fig: bool = False, **kwargs
    ) -> plt.Axes:
        """Plot the image.

        Parameters
        ----------
        ax : matplotlib.Axes instance
            The axis to plot the image to. If None, creates a new figure.
        show : bool
            Whether to actually show the image. Set to false when plotting multiple items.
        clear_fig : bool
            Whether to clear the prior items on the figure before plotting.
        kwargs
            kwargs passed to plt.imshow()
        """
        if ax is None:
            fig, ax = plt.subplots()
        if clear_fig:
            plt.clf()
        ax.imshow(self.array, cmap=get_dicom_cmap(), **kwargs)
        if show:
            plt.show()
        return ax

    def filter(
        self,
        size: float | int = 0.05,
        kind: str = "median",
    ) -> None:
        """Filter the profile in place.

        Parameters
        ----------
        size : int, float
            Size of the median filter to apply.
            If a float, the size is the ratio of the length. Must be in the range 0-1.
            E.g. if size=0.1 for a 1000-element array, the filter will be 100 elements.
            If an int, the filter is the size passed.
        kind : {'median', 'gaussian'}
            The kind of filter to apply. If gaussian, *size* is the sigma value.
        """
        if isinstance(size, float):
            if 0 < size < 1:
                size *= len(self.array)
                size = max(size, 1)
            else:
                raise TypeError("Float was passed but was not between 0 and 1")

        if kind == "median":
            self.array = ndimage.median_filter(self.array, size=size)
        elif kind == "gaussian":
            self.array = ndimage.gaussian_filter(self.array, sigma=size)

    def crop(
        self,
        pixels: int = 15,
        edges: tuple[str, ...] = ("top", "bottom", "left", "right"),
    ) -> None:
        """Removes pixels on all edges of the image in-place.

        Parameters
        ----------
        pixels : int
            Number of pixels to cut off all sides of the image.
        edges : tuple
            Which edges to remove from. Can be any combination of the four edges.
        """
        if pixels < 0:
            raise ValueError("Pixels to remove must be a positive number")
        if "top" in edges:
            self.array = self.array[pixels:, :]
        if "bottom" in edges:
            self.array = self.array[:-pixels, :]
        if "left" in edges:
            self.array = self.array[:, pixels:]
        if "right" in edges:
            self.array = self.array[:, :-pixels]

    def flipud(self) -> None:
        """Flip the image array upside down in-place. Wrapper for np.flipud()"""
        self.array = np.flipud(self.array)

    def fliplr(self) -> None:
        """Flip the image array upside down in-place. Wrapper for np.fliplr()"""
        self.array = np.fliplr(self.array)

    def invert(self) -> None:
        """Invert (imcomplement) the image."""
        orig_array = self.array
        self.array = -orig_array + orig_array.max() + orig_array.min()

    def roll(self, direction: str = "x", amount: int = 1) -> None:
        """Roll the image array around in-place. Wrapper for np.roll().

        Parameters
        ----------
        direction : {'x', 'y'}
            The axis to roll over.
        amount : int
            The amount of elements to roll over.
        """
        axis = 1 if direction == "x" else 0
        self.array = np.roll(self.array, amount, axis=axis)

    def rot90(self, n: int = 1) -> None:
        """Wrapper for numpy.rot90; rotate the array by 90 degrees CCW n times."""
        self.array = np.rot90(self.array, n)

    def threshold(self, threshold: float, kind: str = "high") -> None:
        """Apply a high- or low-pass threshold filter.

        Parameters
        ----------
        threshold : int
            The cutoff value.
        kind : str
            If ``high`` (default), will apply a high-pass threshold. All values above the cutoff are left as-is.
            Remaining points are set to 0.
            If ``low``, will apply a low-pass threshold.
        """
        if kind == "high":
            self.array = np.where(self.array >= threshold, self, 0)
        else:
            self.array = np.where(self.array <= threshold, self, 0)

    def as_binary(self, threshold: int) -> ImageLike:
        """Return a binary (black & white) image based on the given threshold.

        Parameters
        ----------
        threshold : int, float
            The threshold value. If the value is above or equal to the threshold it is set to 1, otherwise to 0.

        Returns
        -------
        ArrayImage
        """
        array = np.where(self.array >= threshold, 1, 0)
        return ArrayImage(array)

    def dist2edge_min(self, point: Point | tuple) -> float:
        """Calculates distance from given point to the closest edge.

        Parameters
        ----------
        point : geometry.Point, tuple

        Returns
        -------
        float
        """
        if isinstance(point, tuple):
            point = Point(point)
        rows = self.shape[0]
        cols = self.shape[1]
        disttoedge = np.zeros(4)
        disttoedge[0] = rows - point.y
        disttoedge[1] = cols - point.x
        disttoedge[2] = point.y
        disttoedge[3] = point.x
        return min(disttoedge)

    def ground(self) -> float:
        """Ground the profile in place such that the lowest value is 0.

        .. note::
            This will also "ground" profiles that are negative or partially-negative.
            For such profiles, be careful that this is the behavior you desire.

        Returns
        -------
        float
            The amount subtracted from the image.
        """
        min_val = self.array.min()
        self.array -= min_val
        return min_val

    def normalize(self, norm_val: str | float = "max") -> None:
        """Normalize the image values in place to the given value.

        Parameters
        ----------
        norm_val : str, number
            If a string, must be 'max', which normalizes the values to the maximum value.
            If a number, normalizes all values to that number.
        """
        if norm_val == "max":
            val = self.array.max()
        else:
            val = norm_val
        self.array = self.array / val

    def check_inversion(
        self, box_size: int = 20, position: (float, float) = (0.0, 0.0)
    ) -> None:
        """Check the image for inversion by sampling the 4 image corners.
        If the average value of the four corners is above the average pixel value, then it is very likely inverted.

        Parameters
        ----------
        box_size : int
            The size in pixels of the corner box to detect inversion.
        position : 2-element sequence
            The location of the sampling boxes.
        """
        row_pos = max(int(position[0] * self.array.shape[0]), 1)
        col_pos = max(int(position[1] * self.array.shape[1]), 1)
        lt_upper = self.array[
            row_pos : row_pos + box_size, col_pos : col_pos + box_size
        ]
        rt_upper = self.array[
            row_pos : row_pos + box_size, -col_pos - box_size : -col_pos
        ]
        lt_lower = self.array[
            -row_pos - box_size : -row_pos, col_pos : col_pos + box_size
        ]
        rt_lower = self.array[
            -row_pos - box_size : -row_pos, -col_pos - box_size : -col_pos
        ]
        avg = np.mean((lt_upper, lt_lower, rt_upper, rt_lower))
        if avg > np.mean(self.array.flatten()):
            self.invert()

    def check_inversion_by_histogram(
        self, percentiles: (float, float, float) = (5, 50, 95)
    ) -> bool:
        """Check the inversion of the image using histogram analysis. The assumption is that the image
        is mostly background-like values and that there is a relatively small amount of dose getting to the image
        (e.g. a picket fence image). This function looks at the distance from one percentile to another to determine
        if the image should be inverted.

        Parameters
        ----------
        percentiles : 3-element tuple
            The 3 percentiles to compare. Default is (5, 50, 95). Recommend using (x, 50, y). To invert the other way
            (where pixel value is *decreasing* with dose, reverse the percentiles, e.g. (95, 50, 5).

        Returns
        -------
        bool: Whether an inversion was performed.
        """
        was_inverted = False
        p_low = np.percentile(self.array, percentiles[0])
        p_mid = np.percentile(self.array, percentiles[1])
        p_high = np.percentile(self.array, percentiles[2])
        mid_to_low = abs(p_mid - p_low)
        mid_to_high = abs(p_mid - p_high)
        if mid_to_low > mid_to_high:
            was_inverted = True
            self.invert()
        return was_inverted

    @argue.bounds(threshold=(0.0, 1.0))
    def gamma(
        self,
        comparison_image: ImageLike,
        doseTA: float = 1,
        distTA: float = 1,
        threshold: float = 0.1,
        ground: bool = True,
        normalize: bool = True,
    ) -> np.ndarray:
        """Calculate the gamma between the current image (reference) and a comparison image.

        .. versionadded:: 1.2

        The gamma calculation is based on `Bakai et al
        <http://iopscience.iop.org/0031-9155/48/21/006/>`_ eq.6,
        which is a quicker alternative to the standard Low gamma equation.

        Parameters
        ----------
        comparison_image : {:class:`~pylinac.core.image.ArrayImage`, :class:`~pylinac.core.image.DicomImage`, or :class:`~pylinac.core.image.FileImage`}
            The comparison image. The image must have the same DPI/DPMM to be comparable.
            The size of the images must also be the same.
        doseTA : int, float
            Dose-to-agreement in percent; e.g. 2 is 2%.
        distTA : int, float
            Distance-to-agreement in mm.
        threshold : float
            The dose threshold percentage of the maximum dose, below which is not analyzed.
            Must be between 0 and 1.
        ground : bool
            Whether to "ground" the image values. If true, this sets both datasets to have the minimum value at 0.
            This can fix offset errors in the data.
        normalize : bool
            Whether to normalize the images. This sets the max value of each image to the same value.

        Returns
        -------
        gamma_map : numpy.ndarray
            The calculated gamma map.

        See Also
        --------
        :func:`~pylinac.core.image.equate_images`
        """
        # error checking
        if not is_close(self.dpi, comparison_image.dpi, delta=0.1):
            raise AttributeError(
                f"The image DPIs to not match: {self.dpi:.2f} vs. {comparison_image.dpi:.2f}"
            )
        same_x = is_close(self.shape[1], comparison_image.shape[1], delta=1.1)
        same_y = is_close(self.shape[0], comparison_image.shape[0], delta=1.1)
        if not (same_x and same_y):
            raise AttributeError(
                f"The images are not the same size: {self.shape} vs. {comparison_image.shape}"
            )

        # set up reference and comparison images
        ref_img = ArrayImage(copy.copy(self.array))
        ref_img.check_inversion_by_histogram()
        if ground:
            ref_img.ground()
        if normalize:
            ref_img.normalize()
        comp_img = ArrayImage(copy.copy(comparison_image.array))
        comp_img.check_inversion_by_histogram()
        if ground:
            comp_img.ground()
        if normalize:
            comp_img.normalize()

        # invalidate dose values below threshold so gamma doesn't calculate over it
        ref_img.array[ref_img < threshold * np.max(ref_img)] = np.NaN

        # convert distance value from mm to pixels
        distTA_pixels = self.dpmm * distTA

        # construct image gradient using sobel filter
        img_x = spf.sobel(ref_img.as_type(np.float32), 1)
        img_y = spf.sobel(ref_img.as_type(np.float32), 0)
        grad_img = np.hypot(img_x, img_y)

        # equation: (measurement - reference) / sqrt ( doseTA^2 + distTA^2 * image_gradient^2 )
        subtracted_img = np.abs(comp_img - ref_img)
        denominator = np.sqrt(
            ((doseTA / 100.0) ** 2) + ((distTA_pixels**2) * (grad_img**2))
        )
        gamma_map = subtracted_img / denominator

        return gamma_map

    def as_type(self, dtype: np.dtype) -> np.ndarray:
        return self.array.astype(dtype)

    @property
    def shape(self) -> (int, int):
        return self.array.shape

    @property
    def size(self) -> int:
        return self.array.size

    @property
    def ndim(self) -> int:
        return self.array.ndim

    @property
    def dtype(self) -> np.dtype:
        return self.array.dtype

    def sum(self) -> float:
        return self.array.sum()

    def ravel(self) -> np.ndarray:
        return self.array.ravel()

    @property
    def flat(self) -> np.ndarray:
        return self.array.flat

    def __len__(self):
        return len(self.array)

    def __getitem__(self, item):
        return self.array[item]


class XIM(BaseImage):
    """A class to open, read, and/or export an .xim image, Varian's custom image format which is 99.999% PNG

    This had inspiration from a number of places:
    - https://gist.github.com/1328/7da697c71f9c4ef12e1e
    - https://medium.com/@duhroach/how-png-works-f1174e3cc7b7
    - https://www.mathworks.com/matlabcentral/answers/419228-how-to-write-for-loop-and-execute-data
    - https://www.w3.org/TR/PNG-Filters.html
    - https://bitbucket.org/dmoderesearchtools/ximreader/src/master/
    """

    array: np.ndarray  #:
    properties: dict  #:

    def __init__(self, file_path: str | Path, read_pixels: bool = True):
        """
        Parameters
        ----------
        file_path
            The path to the file of interest.
        read_pixels
            Whether to read and parse the pixel information. Doing so is quite slow.
            Set this to false if, e.g., you are searching for images only via tags or doing
            a pre-filtering of image selection.
        """
        super().__init__(path=file_path)
        with open(self.path, "rb") as xim:
            self.format_id = decode_binary(xim, str, 8)
            self.format_version = decode_binary(xim, int)
            self.img_width_px = decode_binary(xim, int)
            self.img_height_px = decode_binary(xim, int)
            self.bits_per_pixel = decode_binary(xim, int)
            self.bytes_per_pixel = decode_binary(xim, int)
            self.compression = decode_binary(xim, int)
            if not self.compression:
                pixel_buffer_size = decode_binary(xim, int)
                self.pixel_buffer = decode_binary(
                    xim, str, num_values=pixel_buffer_size
                )
            else:
                lookup_table_size = decode_binary(xim, int)
                self.lookup_table = decode_binary(
                    xim, "B", num_values=lookup_table_size
                )
                comp_pixel_buffer_size = decode_binary(xim, int)
                if read_pixels:
                    lookup_keys = self._parse_lookup_table(self.lookup_table)
                    self.array = self._parse_compressed_bytes(
                        xim, lookup_table=lookup_keys
                    )
                else:
                    _ = decode_binary(xim, "c", num_values=comp_pixel_buffer_size)
                decode_binary(xim, int)
            self.num_hist_bins = decode_binary(xim, int)
            self.histogram = decode_binary(xim, int, num_values=self.num_hist_bins)
            self.num_properties = decode_binary(xim, int)
            self.properties = {}
            for prop in range(self.num_properties):
                name_length = decode_binary(xim, int)
                name = decode_binary(xim, str, num_values=name_length)
                tipe = decode_binary(xim, int)
                if tipe == XIM_PROP_INT:
                    value = decode_binary(xim, int)
                elif tipe == XIM_PROP_DOUBLE:
                    value = decode_binary(xim, "d")
                elif tipe == XIM_PROP_STRING:
                    num_bytes = decode_binary(xim, int)
                    value = decode_binary(xim, str, num_values=num_bytes)
                elif tipe == XIM_PROP_DOUBLE_ARRAY:
                    num_bytes = decode_binary(xim, int)
                    value = decode_binary(
                        xim, "d", num_values=int(num_bytes // 8)
                    )  # doubles are 8 bytes
                elif tipe == XIM_PROP_INT_ARRAY:
                    num_bytes = decode_binary(xim, int)
                    value = decode_binary(
                        xim, int, num_values=int(num_bytes // 4)
                    )  # ints are 4 bytes
                self.properties[name] = value

    @staticmethod
    def _parse_lookup_table(lookup_table_bytes: np.ndarray) -> np.ndarray:
        """The lookup table doesn't follow normal structure conventions like 1, 2, or 4 byte values. They
        got smart and said each value is 2 bits. Yes, bits. This means each byte is actually 4 values.
        Python only reads things as granular as bytes. To get around this the general logic is:

        1) interpret the data as integers at the single byte level
        2) convert those integers back into bit representation; e.g. 115 => 01110011. Note the representation must contain the full byte. I.e. 3 => 11 does not work.
        3) split the binary representation into the 2-bit representations; generates 4x the number of elements. 01110011 => (01, 11, 00, 11)
        4) Convert the 2-bit representation back into integers (01, 11, 00, 11) => (1, 3, 0, 3)

        .. note::

            This is ripe for optimization, but brevity and clarity won out. Options include bit-shifting (fastest)
            and numpy.packbits/unpackbits.
        """
        table = []
        extend = table.extend  # prevent python having to do a lookup on each iteration
        for byte in lookup_table_bytes:
            byte_repr = f"{byte:08b}"
            # didn't actually check these indexes but I think they're right.
            extend(
                [
                    int(byte_repr[6:8], 2),
                    int(byte_repr[4:6], 2),
                    int(byte_repr[2:4], 2),
                    int(byte_repr[0:2], 2),
                ]
            )
        return np.asarray(table, dtype=np.int8)

    def _parse_compressed_bytes(
        self, xim: BinaryIO, lookup_table: np.ndarray
    ) -> np.ndarray:
        """Parse the compressed pixels. We have to do this pixel-by-pixel because each
        pixel can have a different number of bytes representing it

        Per the readme:

        1) The first row is uncompressed
        2) The first element of the second row is uncompressed
        3) all other elements are represented by 1, 2, or 4 bytes of data (the annoying part)
        4) The byte size of the element is given in the lookup table

        So, we have to read in 1, 2, or 4 bytes and convert to an integer depending on
        the lookup table, which tells us how many bytes to read in

        .. note::

            Optimization can help here. A few ideas:

            - reading in groups of data of the same byte size. I already tried this, and I think it will work, but I couldn't get it going.
            - reading in rows of data where no byte change occurred in that row. Similar to above.
            - Using joblib or a processpool
        """
        img_height = self.img_height_px
        img_width = self.img_width_px
        dtype = np.int8 if self.bytes_per_pixel == 1 else np.int16
        compressed_array = a = np.zeros((img_height * img_width), dtype=dtype)
        # first row and 1st element, 2nd row is uncompressed
        # this SHOULD work by reading the # of bytes specified in the header but AFAICT this is just a standard int (4 bytes)
        compressed_array[: img_width + 1] = decode_binary(
            xim, int, num_values=img_width + 1
        )
        diffs = self._get_diffs(lookup_table, xim)
        for diff, idx in zip(
            np.asarray(diffs, dtype=np.int16),
            range(img_width + 1, img_width * img_height),
        ):
            left = a[idx - 1]
            above = a[idx - img_width]
            upper_left = a[idx - img_width - 1]
            a[idx] = diff + left + above - upper_left
        return a.reshape((img_height, img_width))

    @staticmethod
    def _get_diffs(lookup_table: np.ndarray, xim: BinaryIO):
        """Read in all the pixel value 'diffs'. These can be 1, 2, or 4 bytes in size,
        so instead of just reading N pixels of M bytes which would be SOOOO easy, we have to read dynamically

        We optimize here by reading bytes in clumps, which is way faster than reading one at a time.
        Knowing that most values are single bytes with an occasional 2-byte element
        we read chunks that all look like (n 1-bytes and 1 2-byte)
        """
        byte_changes = lookup_table.nonzero()
        byte_changes = np.insert(byte_changes, 0, -1)
        byte_changes = np.append(byte_changes, len(lookup_table) - 1)
        diffs = [5000] * (
            len(lookup_table) - 1
        )  # pre-allocate for speed; 5000 is just for debugging
        LOOKUP_CONVERSION = {0: "b", 1: "h", 2: "i"}
        for start, stop in zip(byte_changes[:-1], byte_changes[1:]):
            if stop - start > 1:
                vals = decode_binary(xim, "b", num_values=stop - start - 1)
                if not isinstance(vals, Iterable):
                    vals = [
                        vals,
                    ]
                diffs[start + 1 : stop] = vals
            if stop != byte_changes[-1]:
                diffs[stop] = decode_binary(xim, LOOKUP_CONVERSION[lookup_table[stop]])
        return diffs

    def save_as(self, file: str, format: str | None = None) -> None:
        """Save the image to a NORMAL format. PNG is highly suggested. Accepts any format supported by Pillow.
        Ironically, an equivalent PNG image (w/ metadata) is ~50% smaller than an .xim image.

        .. warning::

            Any format other than PNG will not include the properties included in the .xim image!

        Parameters
        ----------
        file
            The file to save the image to. E.g. my_xim.png
        format
            The format to save the image as. Uses the Pillow logic, which will infer the format if the file name has one.
        """
        img = pImage.fromarray(self.array)
        # we construct the custom PNG tags; it won't be included for tiff or jpeg, etc but it won't error it either.
        metadata = PngInfo()
        for prop, value in self.properties.items():
            if isinstance(value, np.ndarray):
                value = value.tolist()
            if not isinstance(value, str):
                value = json.dumps(value)
            metadata.add_text(prop, value)
        img.save(file, format=format, pnginfo=metadata)


class DicomImage(BaseImage):
    """An image from a DICOM RTImage file.

    Attributes
    ----------
    metadata : pydicom Dataset
        The dataset of the file as returned by pydicom without pixel data.
    """

    metadata: pydicom.FileDataset
    _sid: float
    _dpi: float
    _sad: float

    def __init__(
        self,
        path: str | Path | BytesIO | BufferedReader,
        *,
        dtype=None,
        dpi: float = None,
        sid: float = None,
        sad: float = 1000,
    ):
        """
        Parameters
        ----------
        path : str, file-object
            The path to the file or the data stream.
        dtype : dtype, None, optional
            The data type to cast the image data as. If None, will use whatever raw image format is.
        dpi : int, float
            The dots-per-inch of the image, defined at isocenter.

            .. note:: If a DPI tag is found in the image, that value will override the parameter, otherwise this one
                will be used.

        sid : int, float
            The Source-to-Image distance in mm.
        """
        super().__init__(path)
        self._sid = sid
        self._dpi = dpi
        self._sad = sad
        # read the file once to get just the DICOM metadata
        self.metadata = retrieve_dicom_file(path)
        self._original_dtype = self.metadata.pixel_array.dtype
        # read a second time to get pixel data
        try:
            path.seek(0)
        except AttributeError:
            pass
        ds = retrieve_dicom_file(path)
        if dtype is not None:
            self.array = ds.pixel_array.astype(dtype)
        else:
            self.array = ds.pixel_array.copy()
        # convert values to HU or CU: real_values = slope * raw + intercept
        has_all_rescale_tags = (
            hasattr(self.metadata, "RescaleSlope")
            and hasattr(self.metadata, "RescaleIntercept")
            and hasattr(self.metadata, "PixelIntensityRelationshipSign")
        )
        has_some_rescale_tags = hasattr(self.metadata, "RescaleSlope") and hasattr(
            self.metadata, "RescaleIntercept"
        )
        is_ct_storage = self.metadata.SOPClassUID.name == "CT Image Storage"
        is_mr_storage = self.metadata.SOPClassUID.name == "MR Image Storage"
        if has_all_rescale_tags:
            self.array = (
                (self.metadata.RescaleSlope * self.array)
                + self.metadata.RescaleIntercept
            ) * self.metadata.PixelIntensityRelationshipSign
        elif is_ct_storage or has_some_rescale_tags:
            self.array = (
                self.metadata.RescaleSlope * self.array
            ) + self.metadata.RescaleIntercept
        elif is_mr_storage:
            # signal is usually correct as-is, no inversion needed
            pass
        else:
            # invert it
            orig_array = self.array
            self.array = -orig_array + orig_array.max() + orig_array.min()

    def save(self, filename: str | Path) -> str | Path:
        """Save the image instance back out to a .dcm file.

        Returns
        -------
        A string pointing to the new filename.
        """
        if self.metadata.SOPClassUID.name == "CT Image Storage":
            self.array = (self.array - int(self.metadata.RescaleIntercept)) / int(
                self.metadata.RescaleSlope
            )
        self.metadata.PixelData = self.array.astype(self._original_dtype).tobytes()
        self.metadata.Columns = self.array.shape[1]
        self.metadata.Rows = self.array.shape[0]
        self.metadata.save_as(filename)
        return filename

    @property
    def sid(self) -> float:
        """The Source-to-Image in mm."""
        try:
            return float(self.metadata.RTImageSID)
        except (AttributeError, ValueError, TypeError):
            return self._sid

    @property
    def sad(self) -> float:
        """The source to axis (iso) in mm"""
        try:
            return float(self.metadata.RadiationMachineSAD)
        except (AttributeError, ValueError, TypeError):
            return self._sad

    @property
    def dpi(self) -> float:
        """The dots-per-inch of the image, defined at isocenter."""
        try:
            return self.dpmm * MM_PER_INCH
        except:
            return self._dpi

    @property
    def dpmm(self) -> float:
        """The Dots-per-mm of the image, defined at isocenter. E.g. if an EPID image is taken at 150cm SID,
        the dpmm will scale back to 100cm."""
        dpmm = None
        for tag in ("PixelSpacing", "ImagePlanePixelSpacing"):
            mmpd = self.metadata.get(tag)
            if mmpd is not None:
                dpmm = 1 / mmpd[0]
                break
        if dpmm is not None and self.sid is not None:
            dpmm *= self.sid / self.sad
        elif dpmm is None and self._dpi is not None:
            dpmm = self._dpi / MM_PER_INCH
        return dpmm

    @property
    def cax(self) -> Point:
        """The position of the beam central axis. If no DICOM translation tags are found then the center is returned.
        Uses this tag: https://dicom.innolitics.com/ciods/rt-beams-delivery-instruction/rt-beams-delivery-instruction/00741020/00741030/3002000d"""
        try:
            x = self.center.x - self.metadata.XRayImageReceptorTranslation[0]
            y = self.center.y - self.metadata.XRayImageReceptorTranslation[1]
        except (AttributeError, ValueError, TypeError):
            return self.center
        else:
            return Point(x, y)


class LinacDicomImage(DicomImage):
    """DICOM image taken on a linac. Also allows passing of gantry/coll/couch values via the filename."""

    gantry_keyword = "Gantry"
    collimator_keyword = "Coll"
    couch_keyword = "Couch"

    _use_filenames: bool

    def __init__(
        self, path: str | Path | BinaryIO, use_filenames: bool = False, **kwargs
    ):
        self._gantry = kwargs.pop("gantry", None)
        self._coll = kwargs.pop("coll", None)
        self._couch = kwargs.pop("couch", None)
        super().__init__(path, **kwargs)
        self._use_filenames = use_filenames

    @property
    def gantry_angle(self) -> float:
        """Gantry angle of the irradiation."""
        if self._gantry is not None:
            return self._gantry
        else:
            return self._get_axis_value(self.gantry_keyword, "GantryAngle")

    @property
    def collimator_angle(self) -> float:
        """Collimator angle of the irradiation."""
        if self._coll is not None:
            return self._coll
        else:
            return self._get_axis_value(
                self.collimator_keyword, "BeamLimitingDeviceAngle"
            )

    @property
    def couch_angle(self) -> float:
        """Couch angle of the irradiation."""
        if self._couch is not None:
            return self._couch
        else:
            return self._get_axis_value(self.couch_keyword, "PatientSupportAngle")

    def _get_axis_value(self, axis_str: str, axis_dcm_attr: str) -> float:
        """Retrieve the value of the axis. This will first look in the file name for the value.
        If not in the filename then it will look in the DICOM metadata. If the value can be found in neither
        then a value of 0 is assumed.

        Parameters
        ----------
        axis_str : str
            The string to look for in the filename.
        axis_dcm_attr : str
            The DICOM attribute that should contain the axis value.

        Returns
        -------
        float
        """
        axis_found = False
        if self._use_filenames:
            filename = osp.basename(self.path)
            # see if the keyword is in the filename
            keyword_in_filename = axis_str.lower() in filename.lower()
            # if it's not there, then assume it's zero
            if not keyword_in_filename:
                axis = 0
                axis_found = True
            # if it is, then make sure it follows the naming convention of <axis###>
            else:
                match = re.search(rf"(?<={axis_str.lower()})\d+", filename.lower())
                if match is None:
                    raise ValueError(
                        f"The filename contains '{axis_str}' but could not read a number following it. Use the format '...{axis_str}<#>...'"
                    )
                else:
                    axis = float(match.group())
                    axis_found = True
        # try to interpret from DICOM data
        if not axis_found:
            try:
                axis = float(getattr(self.metadata, axis_dcm_attr))
            except AttributeError:
                axis = 0
        # if the value is close to 0 or 360 then peg at 0
        if is_close(axis, [0, 360], delta=1):
            return 0
        else:
            return axis


class FileImage(BaseImage):
    """An image from a "regular" file (.tif, .jpg, .bmp).

    Attributes
    ----------
    info : dict
        The info dictionary as generated by Pillow.
    sid : float
        The SID value as passed in upon construction.
    """

    def __init__(
        self,
        path: str | Path | BinaryIO,
        *,
        dpi: float | None = None,
        sid: float | None = None,
        dtype: np.dtype | None = None,
    ):
        """
        Parameters
        ----------
        path : str, file-object
            The path to the file or a data stream.
        dpi : int, float
            The dots-per-inch of the image, defined at isocenter.

            .. note:: If a DPI tag is found in the image, that value will override the parameter, otherwise this one
                will be used.
        sid : int, float
            The Source-to-Image distance in mm.
        dtype : numpy.dtype
            The data type to cast the array as.
        """
        super().__init__(path)
        pil_image = pImage.open(path)
        # convert to gray if need be
        if pil_image.mode not in ("F", "L", "1"):
            pil_image = pil_image.convert("F")
        self.info = pil_image.info
        if dtype is not None:
            self.array = np.array(pil_image, dtype=dtype)
        else:
            self.array = np.array(pil_image)
        self._dpi = dpi
        self.sid = sid

    @property
    def dpi(self) -> float:
        """The dots-per-inch of the image, defined at isocenter."""
        dpi = None
        for key in ("dpi", "resolution"):
            dpi = self.info.get(key)
            if dpi is not None:
                dpi = float(dpi[0])
                break
        if dpi is None:
            dpi = self._dpi
        if self.sid is not None and dpi is not None:
            dpi *= self.sid / 1000
        return dpi

    @property
    def dpmm(self) -> float | None:
        """The Dots-per-mm of the image, defined at isocenter. E.g. if an EPID image is taken at 150cm SID,
        the dpmm will scale back to 100cm."""
        try:
            return self.dpi / MM_PER_INCH
        except TypeError:
            return


class ArrayImage(BaseImage):
    """An image constructed solely from a numpy array."""

    def __init__(
        self,
        array: np.array,
        *,
        dpi: float = None,
        sid: float = None,
        dtype=None,
    ):
        """
        Parameters
        ----------
        array : numpy.ndarray
            The image array.
        dpi : int, float
            The dots-per-inch of the image, defined at isocenter.

            .. note:: If a DPI tag is found in the image, that value will override the parameter, otherwise this one
                will be used.
        sid : int, float
            The Source-to-Image distance in mm.
        dtype : dtype, None, optional
            The data type to cast the image data as. If None, will use whatever raw image format is.
        """
        if dtype is not None:
            self.array = np.array(array, dtype=dtype)
        else:
            self.array = array
        self._dpi = dpi
        self.sid = sid

    @property
    def dpmm(self) -> float | None:
        """The Dots-per-mm of the image, defined at isocenter. E.g. if an EPID image is taken at 150cm SID,
        the dpmm will scale back to 100cm."""
        try:
            return self.dpi / MM_PER_INCH
        except:
            return

    @property
    def dpi(self) -> float | None:
        """The dots-per-inch of the image, defined at isocenter."""
        dpi = None
        if self._dpi is not None:
            dpi = self._dpi
            if self.sid is not None:
                dpi *= self.sid / 1000
        return dpi

    def __sub__(self, other):
        return ArrayImage(self.array - other.array)


class DicomImageStack:
    """A class that loads and holds a stack of DICOM images (e.g. a CT dataset). The class can take
    a folder or zip file and will read CT images. The images must all be the same size. Supports
    indexing to individual images.

    Attributes
    ----------
    images : list
        Holds instances of :class:`~pylinac.core.image.DicomImage`. Can be accessed via index;
        i.e. self[0] == self.images[0].

    Examples
    --------
    Load a folder of Dicom images
    >>> from pylinac import image
    >>> img_folder = r"folder/qa/cbct/june"
    >>> dcm_stack = image.DicomImageStack(img_folder)  # loads and sorts the images
    >>> dcm_stack.plot(3)  # plot the 3rd image

    Load a zip archive
    >>> img_folder_zip = r"archive/qa/cbct/june.zip"  # save space and zip your CBCTs
    >>> dcm_stack = image.DicomImageStack.from_zip(img_folder_zip)

    Load as a certain data type
    >>> dcm_stack_uint32 = image.DicomImageStack(img_folder, dtype=np.uint32)
    """

    images: list[ImageLike]

    def __init__(
        self,
        folder: str | Path,
        dtype: np.dtype | None = None,
        min_number: int = 39,
        check_uid: bool = True,
    ):
        """Load a folder with DICOM CT images.

        Parameters
        ----------
        folder : str
            Path to the folder.
        dtype : dtype, None, optional
            The data type to cast the image data as. If None, will use whatever raw image format is.
        """
        self.images = []
        paths = []
        # load in images in their received order
        if isinstance(folder, (list, tuple)):
            paths = folder
        elif osp.isdir(folder):
            for pdir, sdir, files in os.walk(folder):
                for file in files:
                    paths.append(osp.join(pdir, file))
        for path in paths:
            if self.is_image_slice(path):
                img = DicomImage(path, dtype=dtype)
                self.images.append(img)

        # check that at least 1 image was loaded
        if len(self.images) < 1:
            raise FileNotFoundError(
                f"No files were found in the specified location: {folder}"
            )

        # error checking
        if check_uid:
            self.images = self._check_number_and_get_common_uid_imgs(min_number)
        # sort according to physical order
        self.images.sort(key=lambda x: x.metadata.ImagePositionPatient[-1])

    @classmethod
    def from_zip(cls, zip_path: str | Path, dtype: np.dtype | None = None):
        """Load a DICOM ZIP archive.

        Parameters
        ----------
        zip_path : str
            Path to the ZIP archive.
        dtype : dtype, None, optional
            The data type to cast the image data as. If None, will use whatever raw image format is.
        """
        with TemporaryZipDirectory(zip_path) as tmpzip:
            obj = cls(tmpzip, dtype)
        return obj

    @staticmethod
    def is_image_slice(file: str | Path) -> bool:
        """Test if the file is a CT Image storage DICOM file."""
        try:
            ds = pydicom.dcmread(file, force=True, stop_before_pixels=True)
            return "Image Storage" in ds.SOPClassUID.name
        except (InvalidDicomError, AttributeError, MemoryError):
            return False

    def _check_number_and_get_common_uid_imgs(self, min_number: int) -> list:
        """Check that all the images are from the same study."""
        most_common_uid = Counter(
            i.metadata.SeriesInstanceUID for i in self.images
        ).most_common(1)[0]
        if most_common_uid[1] < min_number:
            raise ValueError(
                "The minimum number images from the same study were not found"
            )
        return [
            i for i in self.images if i.metadata.SeriesInstanceUID == most_common_uid[0]
        ]

    def plot(self, slice: int = 0) -> None:
        """Plot a slice of the DICOM dataset.

        Parameters
        ----------
        slice : int
            The slice to plot.
        """
        self.images[slice].plot()

    def roll(self, direction: str, amount: int):
        for img in self.images:
            img.roll(direction, amount)

    @property
    def metadata(self) -> pydicom.FileDataset:
        """The metadata of the first image; shortcut attribute. Only attributes that are common throughout the stack should be used,
        otherwise the individual image metadata should be used."""
        return self.images[0].metadata

    def __getitem__(self, item) -> DicomImage:
        return self.images[item]

    def __setitem__(self, key, value: DicomImage):
        self.images[key] = value

    def __len__(self):
        return len(self.images)


def gamma_2d(
    reference: np.ndarray,
    evaluation: np.ndarray,
    dose_to_agreement: float = 1,
    distance_to_agreement: int = 1,
    gamma_cap_value: float = 2,
    global_dose: bool = True,
    dose_threshold: float = 5,
    fill_value: float = np.nan,
) -> np.ndarray:
    """Compute a 2D gamma of two 2D numpy arrays. This does NOT do size or spatial resolution checking.
    It performs an element-by-element evaluation. It is the responsibility
    of the caller to ensure the reference and evaluation have comparable spatial resolution.

    The algorithm follows Table I of D. Low's 2004 paper: Evaluation of the gamma dose distribution comparison method: https://aapm.onlinelibrary.wiley.com/doi/epdf/10.1118/1.1598711

    This is similar to the gamma_1d function for profiles, except we must search a 2D grid around the reference point.

    Parameters
    ----------
    reference
        The reference 2D array.
    evaluation
        The evaluation 2D array.
    dose_to_agreement
        The dose to agreement in %. E.g. 1 is 1% of global reference max dose.
    distance_to_agreement
        The distance to agreement in **elements**. E.g. if the value is 4 this means 4 elements from the reference point under calculation.
        Must be >0
    gamma_cap_value
        The value to cap the gamma at. E.g. a gamma of 5.3 will get capped to 2. Useful for displaying data with a consistent range.
    global_dose
        Whether to evaluate the dose to agreement threshold based on the global max or the dose point under evaluation.
    dose_threshold
        The dose threshold as a number between 0 and 100 of the % of max dose under which a gamma is not calculated.
        This is not affected by the global/local dose normalization and the threshold value is evaluated against the global max dose, period.
    fill_value
        The value to give pixels that were not calculated because they were under the dose threshold. Default
        is NaN, but another option would be 0. If NaN, allows the user to calculate mean/median gamma over just the
        evaluated portion and not be skewed by 0's that should not be considered.
    """
    if reference.ndim != 2 or evaluation.ndim != 2:
        raise ValueError(
            f"Reference and evaluation arrays must be 2D. Got reference: {reference.ndim} and evaluation: {evaluation.ndim}"
        )
    threshold = reference.max() / 100 * dose_threshold
    # convert dose to agreement to % of global max; ignored later if local dose
    dose_ta = dose_to_agreement / 100 * reference.max()
    # pad eval array on both edges so our search does not go out of bounds
    eval_padded = np.pad(evaluation, distance_to_agreement, mode="edge")
    # iterate over each reference element, computing distance value and dose value
    gamma = np.zeros(reference.shape)
    for row_idx, row in enumerate(reference):
        for col_idx, ref_point in enumerate(row):
            # skip if below dose threshold
            if ref_point < threshold:
                gamma[row_idx, col_idx] = fill_value
                continue
            # use scikit-image to compute the indices of a disk around the reference point
            # we can then compute gamma over the eval points at these indices
            # unlike the 1D computation, we have to search at an index offset by the distance to agreement
            # we use DTA+1 in disk because it looks like the results are exclusive of edges.
            # https://scikit-image.org/docs/stable/api/skimage.draw.html#disk
            rs, cs = disk(
                (row_idx + distance_to_agreement, col_idx + distance_to_agreement),
                distance_to_agreement + 1,
            )

            capital_gammas = []
            for r, c in zip(rs, cs):
                eval_point = eval_padded[r, c]
                # for the distance, we compare the ref row/col to the eval padded matrix
                # but remember the padded array is padded by DTA, so to compare distances, we
                # have to cancel the offset we used for dose purposes.
                dist = math.dist(
                    (row_idx, col_idx),
                    (r - distance_to_agreement, c - distance_to_agreement),
                )
                dose = eval_point - ref_point
                if not global_dose:
                    dose_ta = dose_to_agreement / 100 * ref_point
                capital_gamma = math.sqrt(
                    dist**2 / distance_to_agreement**2 + dose**2 / dose_ta**2
                )
                capital_gammas.append(capital_gamma)
            gamma[row_idx, col_idx] = min(np.nanmin(capital_gammas), gamma_cap_value)
    return np.asarray(gamma)
