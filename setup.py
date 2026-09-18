"""
Setup file for AWS Waste Hunter.
Allows installation in development mode: pip install -e .
"""
from setuptools import setup, find_packages

setup(
    name="aws-waste-hunter",
    version="2.1.0",
    description="AWS FinOps automation bot for waste detection",
    packages=find_packages(exclude=["tests", "tests.*"]),
    python_requires=">=3.9",
    install_requires=[
        "boto3>=1.34,<2",
        "botocore>=1.34,<2",
        "jinja2>=3.1,<4",
    ],
    extras_require={
        "dev": [
            "pytest>=8,<9",
            "pytest-cov>=5,<7",
            "pytest-mock>=3,<4",
        ],
    },
)
