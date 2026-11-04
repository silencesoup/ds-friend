# 小屋本地 dsh-pet

`upstream/` 是从已安装的 dsh-pet **0.4.1** 复制的完整发布包（不包含 node_modules）。
来源：https://github.com/PC2005-cloud/dsh-pet 。代码版权归 PC2005-cloud，MIT 许可见 `upstream/LICENSE`。
动画素材的原作者声明为**非商用、公开二创需署名**，见上游 README。本目录不是 DSH profile，不读取 DSH 凭据，也不运行它的独立模型或待办。

## 动画是什么

`upstream/assets/webm/` 中有 109 段透明 VP9 WebM 小视频，通常包含约 10 秒的动画。原插件使用 video 播放器，而非逐帧 PNG/GIF 切换。
电脑主聊天页通过 PyAV/libvpx 保留透明通道并逐帧显示；手机网页和悬浮桌宠直接播放 WebM。

## 如何修改

- `animation_descriptions.json`：每个片段的名称、画面描述、适合的心情和话题，以及能否供模型选择。电脑聊天页的“动作图鉴”也可预览并修改描述和话题，保存后立即生效。
- `upstream/assets/config.jsonc`：原插件动画池和动作切换配置。
- `whale_friend/animations.py`：模型动作选择、输出标记过滤、持久化和多端共享。
- `whale_friend/pet_support.py`：把本地原始发布包转换为小屋的独立渲染器；保留原许可。

描述只说明卡通表演，不能把“记笔记”动画视为已经保存记忆，也不能用余额动画伪造账户余额。余额、拖拽、睡眠衔接等片段保留为系统事件，不交给普通聊天随意点播。

默认优先使用小屋更新管理器已激活的版本，再使用本目录的本地发布包，最后寻找 DSH 的安装。手动指定路径时以手动值为准。官方更新包保存在 `.local/plugins/`，**不会覆盖本目录的描述文件或源码**；新增片段在补充描述后才进入模型的可选清单。
动画与回复对应关系随 SQLite 备份。视频原件属于应用资源，不重复放入个人记忆恢复包。
