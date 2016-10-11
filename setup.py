from setuptools import find_packages, setup

setup(
    name='bme-hv-psu',
    version='0.0.1',
    url='https://github.com/klickverbot/bme-hv-psu',
    author='David P. Nadlinger',
    packages=['bme_hv_psu'],
    entry_points={
        'console_scripts': ['bme-hv-psu_controller=bme_hv_psu.artiq_controller:main']
    },
    install_requires=[
		'artiq',
        'llama'
    ]
)
