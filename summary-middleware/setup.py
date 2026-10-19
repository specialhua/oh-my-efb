from setuptools import setup, find_packages

setup(
    name='efb-summary-middleware',
    packages=find_packages(),
    version='0.1.0',
    description='EFB middleware: periodic LLM summary of selected group chats, delivered through ETM.',
    python_requires='>=3.8',
    install_requires=[
        "ehforwarderbot>=2.0.0",
        "efb-ai-middleware",
        "requests",
        "ruamel.yaml",
    ],
    entry_points={
        'ehforwarderbot.middleware': 'specialhua.summary = efb_summary_middleware:SummaryMiddleware',
    }
)
