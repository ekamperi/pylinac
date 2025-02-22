import copy
import math
from unittest import TestCase

from matplotlib import pyplot as plt
from skimage.transform import rotate

from pylinac.cheese import TomoCheese, TomoCheeseResult
from tests_basic.utils import (
    CloudFileMixin,
    FromDemoImageTesterMixin,
    FromURLTesterMixin,
    FromZipTesterMixin,
    InitTesterMixin,
    save_file,
)

TEST_DIR = "Tomo"


class TestInstantiation(
    TestCase,
    InitTesterMixin,
    FromDemoImageTesterMixin,
    FromURLTesterMixin,
    FromZipTesterMixin,
):
    klass = TomoCheese
    init_file = [TEST_DIR, "TomoTherapy Cheese Phantom"]
    demo_load_method = "from_demo_images"
    url = "TomoCheese.zip"
    zip = [TEST_DIR, "TomoCheese.zip"]
    is_folder = True


class TestResults(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cheese = TomoCheese.from_demo_images()
        cls.cheese.analyze()

    def test_results_as_str(self):
        assert isinstance(self.cheese.results(), str)

    def test_results_as_list(self):
        r = self.cheese.results(as_list=True)
        assert isinstance(r, list)

    def test_results_data(self):
        r = self.cheese.results_data()
        assert isinstance(r, TomoCheeseResult)
        assert self.cheese.hu.rois["5"].pixel_value == r.roi_5["median"]
        r = self.cheese.results_data(as_dict=True)
        assert isinstance(r, dict)
        assert self.cheese.hu.rois["9"].std == r["roi_9"]["std"]


class TestGeneral(TestCase):
    def test_demo(self):
        TomoCheese.run_demo()


class TestAnalysis(TestCase):
    def test_cropping_before_analysis(self):
        cheese = TomoCheese.from_demo_images()
        for img in cheese.dicom_stack:
            img.crop(pixels=20, edges=("bottom",))
        # shouldn't raise
        cheese.analyze()

    def test_rolling_before_analysis(self):
        """Rolling (shifting) the phantom by a nominal amount shouldn't affect analysis"""
        cheese = TomoCheese.from_demo_images()
        cheese.analyze()
        original_roi_1 = copy.copy(cheese.hu.rois["1"].pixel_value)
        for img in cheese.dicom_stack:
            img.roll(direction="x", amount=20)
        cheese.analyze()
        new_roi_1 = cheese.hu.rois["1"].pixel_value
        assert math.isclose(original_roi_1, new_roi_1, abs_tol=3)

    def test_rotating_phantom(self):
        """Check that a roll is corrected"""
        cheese = TomoCheese.from_demo_images()
        cheese.analyze()
        assert math.isclose(cheese.catphan_roll, -0.25, abs_tol=0.05)
        for img in cheese.dicom_stack:
            img.array = rotate(img.array, angle=3, mode="edge")
        cheese.analyze()
        assert math.isclose(cheese.catphan_roll, -3.25, abs_tol=0.05)

    def test_too_much_rotation_resets(self):
        # too much roll will reset to 0 however
        cheese = TomoCheese.from_demo_images()
        for img in cheese.dicom_stack:
            img.array = rotate(img.array, angle=13, mode="edge")
        cheese.analyze()
        assert cheese.catphan_roll == 0

    def test_roi_config(self):
        cheese = TomoCheese.from_demo_images()
        config = {"3": {"density": 4.12}}
        cheese.analyze(roi_config=config)
        self.assertEqual(cheese.roi_config, config)


class TestPlottingSaving(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cheese = TomoCheese.from_demo_images()
        cls.cheese.analyze()

    @classmethod
    def tearDownClass(cls):
        plt.close("all")

    def test_save_pdf(self):
        # shouldn't raise
        save_file(self.cheese.publish_pdf, "temp")

    def test_set_figure_size(self):
        self.cheese.plot_analyzed_image(figsize=(8, 13))
        fig = plt.gcf()
        self.assertEqual(fig.bbox_inches.height, 13)
        self.assertEqual(fig.bbox_inches.width, 8)

    def test_save_image(self):
        save_file(self.cheese.save_analyzed_image)

    def test_save_subimage_fails(self):
        """There is no sub-images for the tomo"""
        with self.assertRaises(NotImplementedError):
            self.cheese.save_analyzed_subimage()

    def test_plotting_without_density_fails(self):
        cheese = TomoCheese.from_demo_images()
        cheese.analyze()  # no roi config
        with self.assertRaises(ValueError):
            cheese.plot_density_curve()

    def test_plotting_density(self):
        cheese = TomoCheese.from_demo_images()
        cheese.analyze(roi_config={"1": {"density": 1.2}})
        cheese.plot_density_curve()


class TomoCheeseMixin(CloudFileMixin):
    model = TomoCheese
    origin_slice = 0
    dir_path = ["Tomo"]
    hu_values = {}
    expected_roll = -0.2

    @classmethod
    def setUpClass(cls):
        filename = cls.get_filename()
        cls.cheese = cls.model.from_zip(filename)

    @classmethod
    def tearDownClass(cls):
        plt.close("all")

    def test_slice_locations(self):
        """Test the locations of the slices of interest."""
        self.assertAlmostEqual(self.cheese.origin_slice, self.origin_slice, delta=1)

    def test_phantom_roll(self):
        """Test the roll of the phantom."""
        self.assertAlmostEqual(self.cheese.catphan_roll, self.expected_roll, delta=0.3)

    def test_HU_values(self):
        """Test HU values."""
        for name, roi in self.cheese.hu.rois.items():
            exp_val = self.hu_values[name]
            meas_val = roi.pixel_value
            self.assertAlmostEqual(exp_val, meas_val, delta=5)

    def test_pdf(self):
        save_file(self.cheese.publish_pdf, "temp")


class TomoCheeseDemo(TomoCheeseMixin, TestCase):
    origin_slice = 24
    expected_roll = -0.23
    hu_values = {
        "1": 16,
        "2": 20,
        "3": 23,
        "4": 2,
        "5": 16,
        "6": -669,
        "7": 15,
        "8": 25,
        "9": 653,
        "10": 25,
        "11": 24,
        "12": 102,
        "13": 7,
        "14": -930,
        "15": 23,
        "16": 14,
        "17": -516,
        "18": 447,
        "19": 269,
        "20": 14,
    }

    @classmethod
    def setUpClass(cls):
        cls.cheese = TomoCheese.from_demo_images()
        cls.cheese.analyze()
