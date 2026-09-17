"""reef_sar_adapter - the reef harness adapter for LLaMAR's sar_orch simulator.

One SAR simulation is one reef gate episode: ``reef-sar`` (``runner.py``) runs
``sar_orch/experiment.py`` inside a rendered episode tree and lands the
gradeable artifacts under ``sar/out``; ``trajectory.py`` reads them back into
the trajectory reef hands the method; ``method.py`` scores an episode and
turns an operator's evidence package into tree mutations. ``descriptor.yaml``
declares all of it to reef's shared render and episode engines.

The package is a toolchain, not episode content: it installs into the reef
service environment (``uv pip install -e``) and is never part of the tree an
episode materializes.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["DISTRIBUTION", "__version__", "descriptor", "descriptor_path"]

#: This distribution's name, as its installed metadata reports it.
DISTRIBUTION = "reef-sar-adapter"

__version__ = "0.1.0"


def descriptor_path() -> Path:
    """The ``descriptor.yaml`` shipped beside this module."""
    return Path(__file__).resolve().with_name("descriptor.yaml")


def descriptor():
    """Load this adapter's descriptor: the ``reef.harness_adapters`` entry point.

    Reef's ``external_descriptors()`` loads the entry and calls it when it is
    not already an ``AdapterDescriptor``. The reef import stays inside the
    call so importing this package never requires the service environment.
    """
    from reef.harness.adapters.descriptor import load_descriptor

    return load_descriptor(descriptor_path())
