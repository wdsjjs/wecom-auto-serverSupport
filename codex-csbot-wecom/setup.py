"""Install codex-csbot-wecom on older pip versions."""

from setuptools import find_packages, setup


setup(
    name="codex-csbot-wecom",
    version="0.1.0",
    description="Dual-retrieval customer-service bot using script search and vector memory.",
    packages=find_packages(include=["csbot", "csbot.*"]),
    install_requires=[
        "openpyxl>=3.1.0",
    ],
    entry_points={
        "console_scripts": [
            "csbot=csbot.cli:main",
        ],
    },
    python_requires=">=3.10",
)
