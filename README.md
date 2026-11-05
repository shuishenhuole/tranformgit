# reposor-trans

把 Gitee 仓库迁移到 GitHub 的 Python 脚本。基于 `httpx` + `git`，不依赖 Gitee 的导出功能。

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
pip install httpx      # 唯一的第三方依赖
git --version          # 需要本机有 git
```

## 准备 Token

1. **Gitee**：<https://gitee.com/profile/personal_access_tokens> 新建令牌，勾选 `projects`、`issues`、`pull_requests` 只读即可。
   > Gitee 的仓库列表接口匿名访问基本都会返回 404，所以批量迁移必须配 token。
2. **GitHub**：<https://github.com/settings/tokens>
   - Classic token：勾 `repo`（私有仓库）或 `public_repo`（仅公开仓库）
   - Fine-grained token：`Contents: RW`、`Issues: RW`、`Metadata: R`，需要在组织下建仓库时再加 `Administration: RW`

把 token 写进项目根目录的 `.env`（可复制 `.env.example`），或者直接导出环境变量：

```bash
cp .env.example .env
# 编辑 .env 填入 GITEE_TOKEN / GITHUB_TOKEN
```

## 用法

先自检 token：

```bash
python main.py check
```

看 Gitee 账号下有哪些仓库：

```bash
python main.py list --gitee-owner your-gitee-name
```

迁移单个仓库（目标仓库名默认与源仓库相同）：

```bash
python main.py migrate --gitee your-name/my-repo
```

常见变体：

```bash
# 迁到组织下，并强制设为私有
python main.py migrate --gitee your-name/my-repo --github-owner my-org --visibility private

# 只搬代码，不搬 Issue / Release
python main.py migrate --gitee your-name/my-repo --skip-issues --skip-releases

# 换个仓库名，顺便改简介
python main.py migrate --gitee your-name/my-repo --github-name better-name --description "新简介"

# 批量迁移账号下全部仓库（不含 fork）
python main.py migrate --gitee-owner your-name --all

# 批量里只挑几个
python main.py migrate --gitee-owner your-name --all --only repo-a,repo-b

# 先干跑看看计划，不写任何东西
python main.py migrate --gitee your-name/my-repo --dry-run
```

## 参数速查

| 参数 | 说明 |
| --- | --- |
| `--gitee owner/repo` | 源仓库，也接受 `https://gitee.com/owner/repo.git` |
| `--gitee-owner X --all` | 批量迁移 X 名下所有仓库 |
| `--only a,b` | 批量模式下只迁指定仓库 |
| `--github-owner` | 目标账号/组织，默认当前 token 的登录用户 |
| `--github-name` | 目标仓库名 |
| `--visibility auto\|public\|private` | 默认 `auto`，跟随源仓库 |
| `--skip-git / --skip-issues / --skip-releases` | 跳过对应环节 |
| `--with-assets` | 同时下载并上传 Release 附件（大文件很慢） |
| `--no-annotate` | Issue 正文不加来源说明头 |
| `--allow-duplicates` | 不跳过目标仓库已存在的同名 Issue |
| `--dry-run` | 只打印计划 |
| `--env-file` | 指定 .env 路径 |

默认会跳过目标仓库中已存在同名 Issue，所以中断后重跑是安全的（断点续传）。

## 退出码

`0` 全部成功；`1` 有仓库失败；`2` 参数错误。

## 已知限制

- **Issue 编号会变**：GitHub 重新分配编号，正文里手写的 `#123` 交叉引用不会自动改写（可以按原链接 `gitee.com/...#note_xxx` 反查）。
- **作者归属**：所有 Issue/评论的创建者都会是执行迁移的账号，原作者只写在正文标注里。要完全保真只能用 GitHub 官方 Importer。
- **不迁移**：Pull Request、Wiki、Gitee Pages、仓库成员与权限、Issue 附件图片（Goina 图床外链一般仍可访问）。
- **LFS**：源仓库用了 Git LFS 时，需要本机装了 `git-lfs` 才会拉取真实对象，否则只推指针文件。
- **覆盖行为**：目标仓库同名时直接复用并 `--force` 推送，分支/tag 会被源仓库覆盖。

## 项目结构

```
main.py                CLI 入口
migrator/
  cli.py               argparse、命令分发、结果汇总
  migrate.py           迁移编排（仓库 -> 代码 -> 默认分支 -> Issue -> Release）
  gitee.py             Gitee API v5 封装
  github.py            GitHub REST API 封装
  git_ops.py           git 命令封装（镜像克隆、强推、凭证注入、LFS）
  http.py              httpx 客户端基类（重试、限流退避、分页）
  config.py            极简 .env 读取
```
