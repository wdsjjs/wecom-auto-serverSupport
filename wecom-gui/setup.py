"""Install cli-anything-wecom-gui."""

from setuptools import find_namespace_packages, setup


setup(
    name="cli-anything-wecom-gui",
    version="0.1.0",
    description="CLI-Anything harness for WeCom desktop GUI customer-service automation.",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    include_package_data=True,
    install_requires=[
        "click>=8.0",
        "requests>=2.28",
    ],
    entry_points={
        "console_scripts": [
            "cli-anything-wecom-gui=cli_anything.wecom_gui.wecom_gui_cli:main",
        ],
    },
    python_requires=">=3.10",
)
