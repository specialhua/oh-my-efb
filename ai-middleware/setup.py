from setuptools import setup, find_packages

setup(
    name='efb-ai-middleware',
    packages=find_packages(),
    version='0.1.0',
    description='EFB middleware: shared settings and clients of the AI services used by other middlewares.',
    python_requires='>=3.8',
    install_requires=[
        "ehforwarderbot>=2.0.0",
        "requests",
        "ruamel.yaml",
    ],
    entry_points={
        'ehforwarderbot.middleware': 'specialhua.ai = efb_ai_middleware:AIMiddleware',
    }
)
