---
name: git-workflow
description: 本团队的 git 分支、提交与 PR 规范。改动代码前先读。
---

# Git 工作流

## 分支

- 从 `main` 切分支，命名 `feat/<简述>`、`fix/<简述>`、`chore/<简述>`
- 一个分支只做一件事。发现别的问题，另开分支。
- 不要直接往 `main` 推。

## 提交信息

```
<type>: <一句话说清楚改了什么>

<可选的正文：为什么这么改，而不是改了什么>
```

`type` 取值：`feat` / `fix` / `refactor` / `test` / `docs` / `chore`

规则：

- 标题不超过 50 字符，不加句号
- 正文解释 **why**，不复述 diff
- 不要写 `Co-Authored-By` 之类的署名尾注
- 一次提交对应一个可回滚的逻辑变更

## PR

- 标题同 commit 标题规范
- 描述里必须有：改了什么、怎么验证的、有什么风险
- 自测通过再发起 review
- CI 红的 PR 不进 review 队列

## 常见错误

- `git commit -am` 会把没打算提交的改动一起带上，先 `git status`
- rebase 前先确认没有别人基于你的分支工作
- 不要 `git push --force` 到共享分支，用 `--force-with-lease`
