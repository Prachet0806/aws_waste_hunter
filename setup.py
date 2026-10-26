"""
Setup file for AWS Waste Hunter.
Allows installation in development mode: pip install -e .
"""

from setuptools import find_packages, setup

setup(
    name="aws-waste-hunter",
    version="3.0.0",
    description="AWS FinOps automation bot for waste detection",
    packages=find_packages(exclude=["tests", "tests.*"]),
    py_modules=["lambda_handler"],
    python_requires=">=3.12",
    install_requires=[
        "boto3==1.42.16",
        "botocore==1.42.16",
        "jinja2==3.1.6",
    ],
    extras_require={
        "dev": [
            "pytest>=8,<9",
            "pytest-cov>=5,<7",
            "pytest-mock>=3,<4",
        ],
    },
)
