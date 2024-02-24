from setuptools import setup, find_packages
import os

VERSION = '0.0.1'
DESCRIPTION = 'Python plutus api wrapper'

# Setting up
setup(
    name="plutus-api",
    version=VERSION,
    author="Addenergyx (David Adeniji)",
    description=DESCRIPTION,
    packages=find_packages(),
    install_requires=['pandas', 'numpy', 'pyotp', 'python-dotenv', 'common_shared_library @ git+https://github.com/addenergyx/common-shared-library.git'],
    keywords=['python'],
    classifiers=[
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Operating System :: Unix",
        "Operating System :: MacOS :: MacOS X",
        "Operating System :: Microsoft :: Windows",
    ]
)
