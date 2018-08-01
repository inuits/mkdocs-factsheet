import setuptools

with open("README.md", "r") as fh:
    long_description = fh.read()

with open("requirements.txt", "r") as fh:
    requires = [line.strip() for line in fh]

setuptools.setup(
    name="mkdocs-factsheet",
    version="0.0.1",
    author="Jakub Zárybnický",
    author_email="jakub.zarybnicky@inuits.eu",
    description="Generate overviews from YAML descriptions, intended for micro-services and their deployments",
    url="https://github.com/inuits/mkdocs-factsheet",
    packages=setuptools.find_packages(),
    install_requires=[x for x in requires if x and not x.startswith('#')],
    classifiers=(
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ),
    entry_points={
        'mkdocs.plugins': [
            'factsheet = mkdocs_factsheet.factsheet:FactsheetPlugin',
        ]
    }
)
