import setuptools

setuptools.setup(
    name="pytradekit",
    version="0.0.1",
    description="Basic utilities for pytradekit projects",
    packages=setuptools.find_packages(),
    python_requires=">=3.8",
    install_requires=[
        "pandas",
        "numpy",
        "requests",
        "websockets",
        "python-dotenv",
        "pymongo",
        "redis",
        "slack-sdk",
        "pytest",
        "pytest-cov",
        "pytest-mock",
        "freezegun",
        "concurrent-log-handler",
        "pycryptodome",
        "PyNaCl",
    ]
)
