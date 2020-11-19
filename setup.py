##
# \file setup.py
#
# Instructions:
# 1) `pip install -e .`
#   All python packages and command line tools are then installed during
#
# \author     Marta B.M. Ranzini
# \date       November 2020
#


import re
import os
import sys
from setuptools import setup, find_packages


about = {}
with open(os.path.join("monaifbs", "__about__.py")) as fp:
    exec(fp.read(), about)


with open("README.md", "r") as fh:
    long_description = fh.read()


def install_requires(fname="requirements.txt"):
    with open(fname) as f:
        content = f.readlines()
    content = [x.strip() for x in content]
    return content


setup(name='MONAIfbs',
      version=about["__version__"],
      description=about["__summary__"],
      long_description=long_description,
      long_description_content_type="text/markdown",
      url='https://github.com/martaranzini/MONAIfbs',
      author=about["__author__"],
      author_email=about["__email__"],
      license=about["__license__"],
      packages=find_packages(),
      install_requires=install_requires(),
      zip_safe=False,
      keywords='Fetal brain segmentation with dynUnet',
      classifiers=[
          'Intended Audience :: Developers',
          'Intended Audience :: Healthcare Industry',
          'Intended Audience :: Science/Research',

          'License :: OSI Approved :: Apache 2.0',

          'Topic :: Software Development :: Build Tools',
          'Topic :: Scientific/Engineering :: Medical Science Apps.',

          'Programming Language :: Python',
          'Programming Language :: Python :: 3',
      ],
      )
