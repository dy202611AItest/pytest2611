"""pytest 根目录的 conftest.py 会被自动加载。

`pytest_plugins` 是 pytest 的保留变量，用来声明"还要加载哪些插件模块"。
这里加载的是本项目 plugins/ 下自己写的报告插件（注意写模块路径，不是文件路径）。
"""

pytest_plugins = ["plugins.report_plugin"]
