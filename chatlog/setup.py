from setuptools import setup, find_packages

setup(
    name='efb-chatlog',
    packages=find_packages(),
    package_data={'efb_chatlog': ['static/*']},
    version='0.1.0',
    description='Searchable web log of the chats relayed by EFB, signed in with passkeys.',
    python_requires='>=3.9',
    install_requires=[
        "ehforwarderbot>=2.0.0",
        "fastapi",
        "uvicorn",
        "webauthn>=2.0",
        "requests",
        "ruamel.yaml",
    ],
    entry_points={
        'ehforwarderbot.middleware': 'specialhua.chatlog = efb_chatlog.middleware:ChatlogMiddleware',
        'console_scripts': ['efb-chatlog = efb_chatlog.cli:main'],
    }
)
