# 分享与本地试用

这个目录包含通用 Skill 与可执行 Python 脚本，可独立安装。每位用户使用自己的工作目录、简历资料和专用浏览器登录态。分享公共 Skill，不分享个人求职工作目录。

安装位置与个人数据位置分开：无论 Skill 安装在哪里，默认数据都在调用时工作目录下的 `.boss-workspace/`。新建空目录试用时，只安装 Skill 文件，不预填资料、不复制旧库或登录态；打开新会话后先核对实际工作目录。同一人不同求职方向也应分开数据目录，见 [运行方式](operations.md)。

使用 `python scripts/package_skill.py --out <新ZIP路径>` 按代码中的固定文件清单打包。脚本拒绝覆盖已有 ZIP、拒绝清单内软链接，并生成清单 SHA256。新增要发布的文件需明确加入清单；目录中的其他文件即使误放了简历也不会自动被打包。仍需人工检查通用文件正文，白名单不能证明正文没有被写入私人信息。

给朋友的是 `boss-job-copilot/` 文件夹。依照当前 [官方 Skill 文档](https://learn.chatgpt.com/docs/build-skills)，可将它放在工作项目的 `.agents/skills/` 或用户目录 `~/.agents/skills/`，然后在支持本地 Skill 的 Codex 环境调用 `$boss-job-copilot`。某些已安装版本也使用 `~/.codex/skills/`，以该环境实际发现路径为准，避免同名 Skill 重复安装。若列表未出现，重新开启会话或重启应用。

项目仓库为 https://github.com/visionki/boss-job-copilot，Skill 源目录为 skills/boss-job-copilot。这是本地运行的 Skill；安装 Python 依赖并创建私有目录后运行，详见 operations.md。只测试过 Windows 实机，其他系统尚未实机验证。

本项目采用 GNU AGPL-3.0，分发时保留随 Skill 附带的 LICENSE。Zendriver 等第三方依赖遵循各自许可证。
