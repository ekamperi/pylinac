.. _cheese:

=================
"Cheese" Phantoms
=================

.. versionadded:: 3.9

.. warning::

    These algorithms have only a limited amount of testing data and results should be scrutinized.
    Further, the algorithm is more likely to change in the future when a more robust test suite is built up.
    If you'd like to submit data, enter it `here <https://forms.gle/RBR5ubFvjogE9iC67>`_. Thanks!

The Cheese module provides routines for automatically analyzing
DICOM images of phantoms commonly called "cheese" phantoms, defined
by round phantoms with holes where the user can insert plugs, usually of known density.
The primary use case is performing HU calibration or verification although some plugs
allow for additional functionality such as spatial resolution.
It can load a folder or zip file of images, correcting for
translational and rotational offsets.

Currently, there is only 1 phantom routine for the TomoTherapy cheese phantom,
but similar phantoms will be added to this module in the future.


Running the Demo
----------------

To run one of the Cheese phantom demos, create a script or start an interpreter and input:

.. plot::

    from pylinac import TomoCheese

    TomoCheese.run_demo()

Results will be also be printed to the console::

    - TomoTherapy Cheese Phantom Analysis -
    - HU Module -
    ROI 1 median: 17.0, stdev: 37.9
    ROI 2 median: 20.0, stdev: 44.2
    ROI 3 median: 23.0, stdev: 36.9
    ROI 4 median: 1.0, stdev: 45.7
    ROI 5 median: 17.0, stdev: 37.6
    ROI 6 median: -669.0, stdev: 39.6
    ROI 7 median: 14.5, stdev: 45.8
    ROI 8 median: 26.0, stdev: 38.6
    ROI 9 median: 653.0, stdev: 47.4
    ROI 10 median: 25.0, stdev: 36.7
    ROI 11 median: 24.0, stdev: 35.3
    ROI 12 median: 102.0, stdev: 46.2
    ROI 13 median: 8.0, stdev: 38.1
    ROI 14 median: -930.0, stdev: 43.8
    ROI 15 median: 23.0, stdev: 36.3
    ROI 16 median: 15.0, stdev: 37.1
    ROI 17 median: -516.0, stdev: 45.1
    ROI 18 median: 448.0, stdev: 38.1
    ROI 19 median: 269.0, stdev: 45.3
    ROI 20 median: 15.0, stdev: 37.9


Typical Use
-----------

The cheese phantom analyses follows a similar pattern of load/analyze/output as the rest of the library.
Unlike the CatPhan analysis, tolerances are not applied and comparison to known values is not the goal.
There are two reasons for this: 1) The plugs are interchangable and thus the reference values are not
necessarily constant. 2) Evaluation against a reference is not the end goal as described in the :ref:`philosophy <philosophy>`.
Thus, measured values are provided; what you do with them is your business.

To use the Tomo Cheese analysis, import the class:

.. code-block:: python

    from pylinac import TomoCheese

And then load, analyze, and view the results:

* **Load images** -- Loading can be done with a directory or zip file:

  .. code-block:: python

    cheese_folder = r"C:/TomoTherapy/QA/September"
    cheese = TomoCheese(cheese_folder)

  or load from zip:

  .. code-block:: python

    cheese_zip = r"C:/TomoTherapy/QA/September.zip"
    cheese = TomoCheese.from_zip(cheese_zip)

* **Analyze** -- Analyze the dataset:

  .. code-block:: python

    cheese.analyze()

* **View the results** -- Reviewing the results can be done in text or dictionary format
  as well as images:

  .. code-block:: python

    # print text to the console
    print(cheese.results())
    # return a dictionary or dataclass
    results = cheese.results_data()
    # view analyzed image summary
    cheese.plot_analyzed_image()
    # save the images
    cheese.save_analyzed_image()
    # finally, save a PDF
    cheese.publish_pdf()

.. _plotting_tomo_density:

Plotting density
----------------

An HU-to-density curve can be plotted if an ROI configuration is passed to the ``analyze`` parameter like so:

.. code-block:: python

    import pylinac

    density_info = {'1': {'density': 1.0}, '3': {'density': 3.05}, ...}  # all keys must have a dict with 'density' defined
    tomo = pylinac.TomoCheese(...)
    tomo.analyze(roi_config=density_info)
    tomo.plot_density_curve()  # in this case, ROI 1 and 3 will be plotted vs the stated density

This will plot a simple HU vs density graph.

.. plot::
    :include-source: false

    import pylinac

    density_info = {'1': {'density': 1.0}, '3': {'density': 3.05}}
    tomo = pylinac.TomoCheese.from_demo_images()
    tomo.analyze(roi_config=density_info)
    tomo.plot_density_curve()

.. note::

    The keys of the configuration must be strings matching the ROI number on the phantom. I.e. ``1`` matches to "ROI 1", etc.

.. note::

    Not all ROI densities have to be defined. Any ROI between 1 and 20 can be set.


Algorithm
---------

The TomoCheese algorithm leverages a lot of infrastructure from the CatPhan algorithm.
It is not based on a manual.

Allowances
^^^^^^^^^^

* The images can be any size.
* The phantom can have significant translation in all 3 directions.
* The phantom can have significant roll and moderate yaw and pitch.

Restrictions
^^^^^^^^^^^^

    .. warning:: Analysis can fail or give unreliable results if any Restriction is violated.

* The phantom cannot touch any edge of the FOV.
* There must be at least one ROI in the "outer" ROI ring that is higher than water/background phantom. This
  has to do with the automatic roll compensation.

  .. note::

        This is not strictly required but will assist in accurate sub-degree roll compensation.

Pre-Analysis
^^^^^^^^^^^^

The pre-analysis is almost exactly the same as the :ref:`CatPhan pre-analysis <cbct_pre-analysis>`.

Analysis
^^^^^^^^

* **Determine image properties** -- Automatic roll compensation is attempted by creating a circular
  profile at the radius of the "outer" ROIs. This profile is then searched for peaks, which correspond
  to high-density plugs that have been inserted. If a peak is not found, no correction is applied and the
  phantom is assumed to be at 0.
  This would occur if all plugs have been filled with water/background plugs. If a peak is found,
  i.e. a plug has been inserted with HU detectably above water, the center of the peak is determined
  which would correspond to the center of the ROI. The distance to the nearest nominal ROI is calculated.
  If the value is <5 degrees, the roll compensation is applied. If the value is >5 degrees, the
  compensation is not applied and the phantom is assumed to be at 0.
* **Measure HU values of each plug** -- Based on the nominal spacing and roll compensation (if applied),
  each plug area is sampled for the median and standard deviation values.

API Documentation
-----------------


.. autoclass:: pylinac.cheese.TomoCheese
    :inherited-members:
    :members:

.. autoclass:: pylinac.cheese.TomoCheeseModule
    :inherited-members:
    :members:

.. autoclass:: pylinac.cheese.TomoCheeseResult
    :inherited-members:
    :members:
