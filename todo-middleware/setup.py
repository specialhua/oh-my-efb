from setuptools import setup, find_packages

setup(
    name='efb-todo-middleware',
    packages=find_packages(exclude=["tests"]),
    version='0.1.0',
    description='EFB middleware: to-dos made from messages in Telegram, reminded of by the ETM bot.',
    python_requires='>=3.9',
    install_requires=[
        "ehforwarderbot>=2.0.0",
        "efb-ai-middleware",
        "ruamel.yaml",
        "tzdata",
    ],
    entry_points={
        'ehforwarderbot.middleware': 'specialhua.todo = efb_todo_middleware:TodoMiddleware',
    }
)
