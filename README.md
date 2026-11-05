# reposor-trans

把 Gitee 仓库迁移到 GitHub 的命令行工具。Python + `Typer` + `httpx` + `git`，不依赖 Gitee 的导出功能。

## 迁移内容

| 内容 | 说明 |
| --- | --- |
| 代码 | `git clone --mirror` 源仓库，把所有分支和 tag 强推到 GitHub |
| 默认分支 | 自动对齐源仓库的默认分支 |
| 仓库属性 | 名称、简介、可见性（公开/私有，可覆盖） |
| Issue | 标题、正文、标签（保留颜色）、开放/关闭状态、全部评论 |
| Release | tag 关联的 Release 名称、说明、预发布标记；附件可选搬运 |

Issue 正文会加上来源说明头，评论会标注原作者和原始时间，因为 GitHub API 不允许伪造创建时间：

```
> 🔁 该 Issue 迁移自 Gitee ｜ 原作者：@alice ｜ 原状态：closed ｜ 原创建时间：2020-01-02 03:04 ｜ 原链接：https://gitee.com/...
```

## 安装

```bash
pip install -e .        # 装依赖（typer、httpx），并生成 reposor-trans 命令
# 或者只装依赖，用 python main.py 运行：
pip install typer httpx
git --version           # 需要本机有 git
```

## 准备 Token

1. **Gitee**：<https://gitee.com/profile/personal_access_tokens> 新建令牌，勾选 `projects`、`issues`、`pull_requests` 只读即可。
   > Gitee 的仓库列表接口匿名访问基本都会返回 404，想「一键列出全部仓库」必须配 token。
2. **GitHub**：<https://github.com/settings/tokens>
   - Classic token：勾 `repo`（私有仓库）或 `public_repo`（仅公开仓库）
   - Fine-grained token：`Contents: RW`、`Issues: RW`、`Metadata: R`，需要在组织下建仓库时再加 `Administration: RW`

把 token 写进项目根目录的 `.env`（可复制 `.env.example`），或者直接导出环境变量：

```bash
cp .env.example .env
# 编辑 .env 填入 GITEE_TOKEN / GITHUB_TOKEN
```

## 一键全量

```bash
python main.py list              # 列出 token 能访问的全部仓库（个人 + 所属组织）
python main.py migrate --all     # 把上面这些仓库全迁到 GitHub
```

`--all` 会先从 `/user/repos` 取个人仓库，再遍历 `/user/orgs` 逐个组织取仓库，按 `full_name` 去重后依次迁移。仓库多于一个时会先弹一次确认，管道/CI 环境自动跳过确认（也可以加 `-y` 显式跳过）。

## 用法

```bash
python main.py --help            # 总览
python main.py migrate --help    # 子命令的所有参数
```

**自检 token**

```bash
python main.py check
```

**看仓库清单**

```bash
python main.py list                    # 全部仓库
python main.py list -o your-name       # 只看某个账号/组织
python main.py list --json             # JSON 输出，便于脚本处理
python main.py list --forks            # 包含 fork
```

**迁移单个仓库**（目标仓库名默认与源仓库相同）

```bash
python main.py migrate your-name/my-repo
```

**常见变体**

```bash
# 迁到组织下，并强制设为私有
python main.py migrate your-name/my-repo --github-owner my-org --visibility private

# 只搬代码，不搬 Issue / Release
python main.py migrate your-name/my-repo --skip-issues --skip-releases

# 换个仓库名，顺便改简介
python main.py migrate your-name/my-repo --github-name better-name --description "新简介"

# 只迁某个组织下的仓库
python main.py migrate --all -o my-org

# 全量里只挑几个
python main.py migrate --all --only repo-a,repo-b

# 先干跑看看计划，不写任何东西
python main.py migrate your-name/my-repo --dry-run
```

## 参数速查

| 参数 | 说明 |
| --- | --- |
| `REPO`（位置参数） | 源仓库，写 `owner/repo`，也接受 `https://gitee.com/owner/repo.git` |
| `--all, -a` | 一键迁移当前账号全部仓库（含所属组织） |
| `--owner, -o` | 只处理该账号/组织的仓库（配合 `--all`） |
| `--only a,b` | 批量模式下只迁指定仓库名 |
| `--forks` | 批量时包含 fork |
| `--github-owner` | 目标账号/组织，默认当前 token 的登录用户 |
| `--github-name` | 目标仓库名 |
| `--visibility auto\|public\|private` | 默认 `auto`，跟随源仓库 |
| `--skip-git / --skip-issues / --skip-releases` | 跳过对应环节 |
| `--with-assets` | 同时下载并上传 Release 附件（大文件很慢） |
| `--annotate / --no-annotate` | Issue 正文是否加来源说明头（默认加） |
| `--allow-duplicates` | 不跳过目标仓库已存在的同名 Issue |
| `--dry-run` | 只打印计划 |
| `--yes, -y` | 批量迁移时跳过确认 |
| `--env-file` | 指定 .env 路径 |

默认会跳过目标仓库中已存在同名 Issue，所以中断后重跑是安全的（断点续传）。

## 退出码

| 码 | 含义 |
| --- | --- |
| `0` | 全部成功 |
| `1` | 有仓库失败，或 token 无效等致命错误 |
| `2` | 参数用法错误 |
| `130` | 用户 Ctrl+C 中断 |

## 已知限制

- **Issue 编号会变**：GitHub 重新分配编号，正文里手写的 `#123` 交叉引用不会自动改写（可以按原链接 `gitee.com/...#note_xxx` 反查）。
- **作者归属**：所有 Issue/评论的创建者都会是执行迁移的账号，原作者只写在正文标注里。要完全保真只能用 GitHub 官方 Importer。
- **不迁移**：Pull Request、Wiki、Gitee Pages、仓库成员与权限、Issue 附件图片（图床外链一般仍可访问）。
- **LFS**：源仓库用了 Git LFS 时，需要本机装了 `git-lfs` 才会拉取真实对象，否则只推指针文件。
- **覆盖行为**：目标仓库同名时直接复用并 `--force` 推送，分支/tag 会被源仓库覆盖。
- **组织仓库**：Gitee 的 `/user/repos` 不含组织仓库，所以 `--all` 会额外遍历组织；没有权限的组织会被跳过并提示。

## 项目结构

```
main.py                CLI 入口（把退出码透传给 shell）
migrator/
  cli.py               Typer 子命令、批量调度、结果汇总
  migrate.py           迁移编排（仓库 -> 代码 -> 默认分支 -> Issue -> Release）
  gitee.py             Gitee API v5 封装（含「列出全部仓库」）
  github.py            GitHub REST API 封装
  git_ops.py           git 命令封装（镜像克隆、强推、凭证注入、LFS）
  http.py              httpx 客户端基类（重试、限流退避、分页）
  config.py            极简 .env 读取
```
