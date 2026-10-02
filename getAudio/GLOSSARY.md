# Verbatim

Verbatim 把音视频转成文字，把一个人说过的话整理成能追溯到原话那一秒的证据，再让人带着出处去问、去整理。
这份是项目的统一叫法：代码、接口、界面文案、和 AI 说话都用这里的词。每条后面的 `代码里` 是现有代码用的名字
（历史原因，有的跟界面叫法不一样；新代码优先用左边的词）。

## 组织

**项目**：
一组放在一起研究的来源，以及从它们做出来的东西；首页上的一张卡片。一个项目可以有好几个博主、文档、录音。
代码里：chain（`results/_chains/<id>/`、`/api/chain/<id>`）。
_Avoid_：笔记本、链条（只在说存储时用）

**合集**：
几个项目放在一起的分组，只是首页上的筛选，不是一种项目。
代码里：`_prefs.json` 的 `collections`、接口 `/api/groups`。
_Avoid_：分组、group（界面上）；也别跟下面的「旧式合集项目」混

**旧式合集项目**：
早期 `kind='collection'` 的项目（把别处的转写拼在一起抽卡、写综述）。界面上跟项目一样显示，只为兼容保留。
代码里：`kind == 'collection'`、`/api/collections`。

**博主**：
有频道链接的那个人（YouTube / B 站频道）。博主的转写、证据卡、画像在他自己的链条里只算一次，放进几个项目都是共用。
代码里：`url` 不为空的 chain、`kind` 缺省（'creator'）。
_Avoid_：创作者、KOL、channel（说人的时候）

**人**：
项目里能「按人看」的对象：项目自己的频道、引用进来的博主，或抽过卡的一批零散录音。画像、镜头、立场、预测、原话跟着人走。
代码里：`people` / `person` / `/api/chain/<id>/people`。
_Avoid_：persona（只在「用他的口吻回答」时用）、creator（card_view 里的显示字段除外）

**说话人**：
录音里开口说话的某一个人，按声纹认出来；在整个项目里叫法固定——同一个名字就是同一个人（「祖老师」「学生3」）。
跟「人」不是一回事：人是能按人看画像的博主，说话人是录音里的声音。说得最多的那位是「主讲」。
代码里：`voices.py`、项目目录的 `voices.json` 里的 `speakers`、`/api/chain/<id>/voices`；证据卡上的 `speaker`。
_Avoid_：说话人1 / 说话人2 当成跨录音的身份（那是转写引擎在一份文件里的临时编号）、声音（单说时）

**引用**：
把一个已有的博主加进另一个项目的方式：只记他的链条 id 和一个固定字母，不拷贝数据。
代码里：`sources.json` 的 `channels: [{chain_id, tag}]`。

**字母**：
引用的博主在项目里的固定编号（B、C……）。他的卡片 id 写成 `B:3-12`；拿掉的字母不再发给别人（`retired_tags`）。见 ADR-0001。
_Avoid_：前缀、letter（对比功能里的 A–F 是另一套，只在一次对比结果内有效）

## 来源

**来源**：
项目里提问和生成时能用的材料：一期视频、一段录音、一份文档或网页。左栏的勾选决定用哪些。
代码里：videos（频道的期 / 没频道的项目里的录音）、`sources.json` 的 recordings、docs。

**期**：
一个视频或一段录音，转写后的单位。给人看的编号是 EP3；文档是 DOC2。
代码里：episode、video、task（转写任务 id = task_id）。
_Avoid_：集、episode 之外的叫法

**转写**：
一期的逐段文字，带时间点。存在 `results/<task_id>/transcript.json`，元信息在 `meta.json`。
_Avoid_：字幕（只指平台自带的字幕轨）

**文档**：
用户加的 PDF / Word / 网页 / 粘贴的文字，转成 Markdown 后切段。全局存在 `results/_docs/<doc_id>/`，项目只登记 id。

## 证据

**证据卡**：
从一期里抽出来的一条：原话（quote，一字不改）+ AI 写的观察（obs）+ 时间点 + 层级（主张 / 自证 / 核实）。
id 是「文件编号-下标」：`3-12`。
代码里：card、`cards_NNN.json`。
_Avoid_：卡片（单说时可以）、quote card

**原文段落**：
文档或转写原样切出来的一段，不经 AI 挑选；提问时跟证据卡一起检索。id 是 `d<固定号>-<段>` / `t<固定号>-<段>`。
代码里：passage、`layer == 'source'` 的卡。

**出处**：
回答 / 报告里每句话后面的 `[#id]`，界面上是数字小圆点，点开能看原话、跳到那一秒或那一页。
代码里：citation、cite。
_Avoid_：引用（容易跟「引用博主」混）、来源（那是材料本身）

**语料**：
一次提问或生成时能检索的全部卡片和段落（项目自己的 + 引用博主的）。
代码里：`ask.load()` 返回的 corpus。

## 分析

**画像**：
一个人的综合解读（总分析.md）。没频道的项目里叫**综述**。
代码里：`final_doc`、portrait、overview。

**镜头**：
从同一批证据卡换个角度写的短文：锐评、手艺、好不好看、金句、立场一览。
代码里：lens、`镜头_<key>.md`。

**立场**：
按话题看一个人是支持还是反对、前后有没有变。代码里：topics、stance、`tags.json`。

**预测**：
他说过的、能被后来事实检验的判断，以及核对结果。代码里：predictions、`predictions.json`。

**核心信念**：
同一个主张在 ≥3 期里反复出现。代码里：beliefs、`beliefs.json`。

**同步**：
定期重新探测频道、只处理新视频，并把新内容并进画像。
代码里：subscription、`subscription.json`、`_sub_*`。
_Avoid_：订阅（界面已改叫同步）

## 工作台

**工作台**：
项目页右栏：上面的格子是「做一份新的」，下面的列表是做好的东西。
代码里：studio、`static/study.js`、`study.py`。
_Avoid_：复习区

**产出**：
工作台列表里的一条：报告、闪卡、自测题、对照检查、存下的回答、对比，以及「谁的立场 / 预测 / 原话」。
代码里：`studio/<id>.json`，kind = report | flashcards | quiz | coverage | note | compare | view。

**对比**：
同一个问题，项目里 2–4 个人各怎么说，并排、各带出处。代码里：compare、`ask.compare`。

## 转写

**引擎**：
把音频转成文字的方式：gemini35、gemini、qwen（dashscope）、whisper、precise。代码里：engine、`transcribe_*.py`。

**任务**：
一次转写（或下载后转写）的排队和执行。代码里：task、`taskdb`、`tasks[id]`。
