"""
Setup file for AWS Waste Hunter.
Allows installation in development mode: pip install -e .
"""
from setuptools import setup, find_packages

setup(
    name="aws-waste-hunter",
    version="1.0.0",
    description="AWS FinOps automation bot for waste detection",
    packages=find_packages(exclude=["tests", "tests.*"]),
    python_requires=">=3.9",
    install_requires=[
        "boto3",
        "jinja2",
    ],
    extras_require={
        "dev": [
            "pytest",
            "pytest-cov",
            "pytest-mock",
        ],
    },
)
