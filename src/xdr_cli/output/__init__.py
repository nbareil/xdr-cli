__all__ = ["write_xql_results_excel"]


def __getattr__(name):
    if name == "write_xql_results_excel":
        from .excel import write_xql_results_excel

        return write_xql_results_excel
    raise AttributeError(name)
