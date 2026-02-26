__all__ = ["create_slack_app", "SlackXdrDispatcher"]


def __getattr__(name):
    if name == "create_slack_app":
        from .app import create_slack_app

        return create_slack_app
    if name == "SlackXdrDispatcher":
        from .router import SlackXdrDispatcher

        return SlackXdrDispatcher
    raise AttributeError(name)
