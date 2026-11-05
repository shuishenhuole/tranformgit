"""reposor-trans: Gitee -> GitHub 仓库迁移工具。

模块划分（按依赖方向从上到下）：
    cli       命令行解析、批量调度、结果汇总
    migrate   迁移流程编排（仓库 -> 代码 -> 默认分支 -> Issue -> Release）
    gitee     Gitee OpenAPI v5 封装
    github    GitHub REST API 封装
    git_ops   git 命令封装（镜像克隆、强推、凭证注入）
    http      httpx 客户端基类（重试、限流退避、分页）
    config    .env 读取

整体思路：代码部分交给 git 自己搬（clone --mirror + push），
Issue / Release 这类 git 管不到的数据走两边的 REST API。
"""

__version__ = "0.1.0"
