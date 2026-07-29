from dataclasses import dataclass, field

from evals.moderation.policy_snapshot import PolicyDefinition, build_policy_snapshot
from evals.moderation.schemas import (
    EvalAnnotation,
    EvalAnnotationStatus,
    EvalCaseSource,
    EvalContext,
    EvalDatasetSplit,
    EvalEvidenceField,
    EvalEvidenceSpan,
    EvalInput,
    EvalLabel,
    EvalPrivacyDeclaration,
    EvalPrivacyMode,
    ModerationEvalCase,
)
from moderation.schemas import ModerationAction, RiskType
from moderation.services.policies import DEFAULT_POLICIES


@dataclass(frozen=True, slots=True)
class SeedEvidence:
    text: str
    field: EvalEvidenceField = EvalEvidenceField.CONTENT
    source_index: int | None = None


@dataclass(frozen=True, slots=True)
class SeedVariant:
    kind: str
    content: str
    evidence: tuple[SeedEvidence, ...]
    reason: str
    safe: bool
    action: ModerationAction
    context: EvalContext = field(default_factory=EvalContext)
    acceptable_actions: tuple[ModerationAction, ...] = ()
    synthetic_sensitive_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SeedScenarioGroup:
    key: str
    policy_code: str
    risk_type: RiskType
    variants: tuple[SeedVariant, SeedVariant, SeedVariant, SeedVariant]


def _e(
    text: str,
    field: EvalEvidenceField = EvalEvidenceField.CONTENT,
    source_index: int | None = None,
) -> SeedEvidence:
    return SeedEvidence(text=text, field=field, source_index=source_index)


def _safe(
    content: str,
    evidence: str,
    reason: str,
    *,
    context: EvalContext | None = None,
    privacy: tuple[str, ...] = (),
) -> SeedVariant:
    return SeedVariant(
        kind="safe-exclusion",
        content=content,
        evidence=(_e(evidence),),
        reason=reason,
        safe=True,
        action=ModerationAction.PASS,
        context=context or EvalContext(),
        synthetic_sensitive_values=privacy,
    )


def _risk(
    kind: str,
    content: str,
    evidence: SeedEvidence | str,
    reason: str,
    *,
    context: EvalContext | None = None,
    action: ModerationAction = ModerationAction.REJECT,
    acceptable: tuple[ModerationAction, ...] = (),
    privacy: tuple[str, ...] = (),
) -> SeedVariant:
    return SeedVariant(
        kind=kind,
        content=content,
        evidence=(evidence if isinstance(evidence, SeedEvidence) else _e(evidence),),
        reason=reason,
        safe=False,
        action=action,
        context=context or EvalContext(),
        acceptable_actions=acceptable,
        synthetic_sensitive_values=privacy,
    )


SEED_SCENARIO_GROUPS: tuple[SeedScenarioGroup, ...] = (
    SeedScenarioGroup(
        key="ad-course-sale",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "这门 Python 课程的第三章讲得怎么样？想听听学过的人评价。",
                "想听听学过的人评价",
                "询问课程体验，没有销售、引流或交易意图。",
            ),
            _risk(
                "direct",
                "Python 冲刺课今天五折，想报名的直接私信我下单。",
                "直接私信我下单",
                "以折扣和私聊下单推动交易。",
            ),
            _risk(
                "obfuscated",
                "资料名额只剩三个，评区留“要”，我发购 买 入 口。",
                "我发购 买 入 口",
                "通过拆字和评论暗号规避广告识别。",
            ),
            _risk(
                "context-dependent",
                "这套资料我这里还有，需要的话按老办法联系。",
                "按老办法联系",
                "“老办法”可能指既有交易渠道，但上下文不足，需要人工确认。",
                context=EvalContext(
                    parent_comment="你说的是之前那套资料吗？",
                    complete=False,
                ),
                action=ModerationAction.HUMAN_REVIEW,
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-off-platform",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "文章在比较微信支付和银行卡支付的技术流程，没有留下联系方式。",
                "比较微信支付和银行卡支付",
                "通信平台仅作为讨论对象，不构成引流。",
            ),
            _risk(
                "direct",
                "平台不方便报价，加我微信后发你最低价。",
                "加我微信后发你最低价",
                "用外部联系方式承接报价和交易。",
            ),
            _risk(
                "obfuscated",
                "想拿内部价就加薇，号在昵称里，备注“团购”。",
                "加薇",
                "用同音字隐藏站外导流。",
            ),
            _risk(
                "profile-diversion",
                "联系方式放在个人简介，价格和库存都私聊。",
                "价格和库存都私聊",
                "将交易沟通导向个人资料中的站外渠道。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-referral-link",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "提醒大家不要点击陌生返利链接，先核对官方域名。",
                "不要点击陌生返利链接",
                "这是反诈骗提醒，不是在推广链接。",
            ),
            _risk(
                "direct",
                "注册送二十元，走我的邀请链接：https://example.com/ref/demo123",
                "走我的邀请链接",
                "以注册奖励推广个人邀请链接。",
            ),
            _risk(
                "obfuscated",
                "返现入口写成 example[点]com/r/abc，复制后把“点”换掉。",
                "返现入口",
                "通过替换域名符号隐藏返利推广。",
            ),
            _risk(
                "commission",
                "每邀请一人我和你都拿佣金，链接在置顶评论。",
                "每邀请一人我和你都拿佣金",
                "佣金激励和链接入口共同构成推广。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-repetition-history",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "我买的这款键盘空格键偏松，有没有同型号用户遇到过？",
                "空格键偏松",
                "真实使用问题讨论，没有销售目的。",
            ),
            _risk(
                "history",
                "今天还是这个福利，想要的照旧点我头像。",
                _e(
                    "限时福利，点头像购买",
                    EvalEvidenceField.AUTHOR_RECENT_CONTENT,
                    0,
                ),
                "近期内容显示同一推广被反复发布。",
                context=EvalContext(
                    author_recent_contents=[
                        "限时福利，点头像购买",
                        "最后十份，点头像购买",
                    ]
                ),
            ),
            _risk(
                "template-spam",
                "【库存更新】A 套餐 99，B 套餐 159，拍下当天发货。",
                "拍下当天发货",
                "模板化报价并直接促成购买。",
            ),
            _risk(
                "cross-post",
                "换个帖子继续发：需要代办服务的留言，我逐个联系。",
                "需要代办服务的留言",
                "跨帖子重复招揽服务客户。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-recruitment-commission",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "这份招聘启事没有写劳动合同，大家觉得有哪些风险？",
                "有哪些风险",
                "讨论招聘信息风险，不是在招揽人员。",
            ),
            _risk(
                "direct",
                "招校园代理，不囤货，每拉一单返你三成。",
                "每拉一单返你三成",
                "通过佣金招募销售代理。",
            ),
            _risk(
                "pyramid",
                "发展两个下级就能升级合伙人，团队流水还有分红。",
                "发展两个下级",
                "以发展下级和团队分红进行推广招募。",
            ),
            _risk(
                "coded",
                "找几个一起做副业的伙伴，具体收益模型进群再讲。",
                "具体收益模型进群再讲",
                "用模糊副业表述将用户导向营销群。",
                acceptable=(ModerationAction.REJECT, ModerationAction.LIMIT),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-livestream-diversion",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "昨晚直播里演示的压缩算法，有人整理技术笔记吗？",
                "整理技术笔记",
                "围绕直播内容进行技术交流。",
            ),
            _risk(
                "direct",
                "今晚八点来我直播间，前五十名下单送同款。",
                "前五十名下单送同款",
                "用赠品和下单刺激为直播带货引流。",
            ),
            _risk(
                "platform-code",
                "这里不说价，去短视频平台搜“北岸好物”看橱窗。",
                "看橱窗",
                "引导用户去其他平台商品橱窗。",
            ),
            _risk(
                "countdown",
                "直播只开十分钟，错过恢复原价，主页入口速进。",
                "错过恢复原价",
                "利用限时价格和主页入口促成交易。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-engagement-giveaway",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "社区周年活动的抽奖规则是否允许重复参与？",
                "抽奖规则是否允许重复参与",
                "询问平台活动规则，没有私域营销。",
            ),
            _risk(
                "engagement-bait",
                "点赞、关注再转发，截图私信我就能领试用装。",
                "点赞、关注再转发",
                "以赠品换取互动和私信导流。",
            ),
            _risk(
                "comment-code",
                "评论区打 888，我抽十个人送商品券，顺便关注店铺。",
                "顺便关注店铺",
                "通过口令抽奖为店铺拉新。",
            ),
            _risk(
                "group-diversion",
                "赠品名单只在粉丝群公布，入群方式见主页。",
                "入群方式见主页",
                "用赠品将用户导向外部粉丝群。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-homophone-code",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "“薇”和“微”在古诗用字里有什么差别？",
                "古诗用字",
                "同音字用于语言讨论，不含联系方式或交易。",
            ),
            _risk(
                "homophone",
                "要报价就➕薇，搜昵称同名账号。",
                "要报价就➕薇",
                "用同音字和符号隐藏站外询价。",
            ),
            _risk(
                "spaced",
                "全网低价，联 系 方 式 看 签 名。",
                "联 系 方 式 看 签 名",
                "拆分关键词规避广告检测并导向签名。",
            ),
            _risk(
                "emoji-code",
                "想买的扣🛒，我按留言顺序发暗号。",
                "想买的扣🛒",
                "用表情和暗号组织购买。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="ad-resale-transaction",
        policy_code="ADV-001",
        risk_type=RiskType.ADVERTISING,
        variants=(
            _safe(
                "二手交易时如何判断演出票是否已经核销？",
                "如何判断",
                "询问交易安全知识，没有发布商品。",
            ),
            _risk(
                "ticket-sale",
                "出两张周六演出票，原价转，确定要的私聊付款。",
                "私聊付款",
                "发布票务并要求私聊交易。",
            ),
            _risk(
                "service-sale",
                "可代抢热门场次，成功后收费，先付订金排队。",
                "先付订金排队",
                "推销收费代抢服务。",
            ),
            _risk(
                "inventory",
                "还有五个兑换码，单个八十，打包价格可谈。",
                "单个八十",
                "明确报价并出售兑换码。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-targeted-insult",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "我不同意你的方案，因为成本估算漏掉了运维费用。",
                "成本估算漏掉了运维费用",
                "批评具体方案，没有攻击个人。",
            ),
            _risk(
                "direct",
                "你连这点代码都看不懂，真是个废物。",
                "真是个废物",
                "贬损性称呼直接针对对话对象。",
            ),
            _risk(
                "sarcastic",
                "某位天才又把生产库删了，脑子大概也一起清空了。",
                "脑子大概也一起清空了",
                "以讽刺方式对特定同事进行人格羞辱。",
            ),
            _risk(
                "coded",
                "不点名了，三楼那位的智商大家都懂。",
                "三楼那位的智商大家都懂",
                "虽未写姓名，但目标可识别且含侮辱。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-threat",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "电影台词“我会找到你”放在这个剧情里是在制造悬念。",
                "电影台词",
                "威胁性语句被明确作为虚构作品分析。",
            ),
            _risk(
                "direct",
                "你再发一次，我明天就在校门口等你算账。",
                "在校门口等你算账",
                "包含具体时间地点的定向威胁。",
            ),
            _risk(
                "implicit",
                "我知道你每天几点下班，最好从现在开始小心点。",
                "最好从现在开始小心点",
                "用掌握行踪暗示现实伤害。",
            ),
            _risk(
                "ambiguous-context",
                "这事没完，见面你就知道了。",
                "见面你就知道了",
                "表述可能是威胁，但缺少前文和行为信息。",
                context=EvalContext(complete=False),
                action=ModerationAction.HUMAN_REVIEW,
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-repeated-harassment",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "提醒一下，你提交的附件还缺签名，请补齐后再发。",
                "附件还缺签名",
                "一次正常的工作提醒。",
            ),
            _risk(
                "history",
                "怎么还不回？我会一直在每个帖子下面提醒你。",
                _e(
                    "你躲着也没用，我每天都会来问",
                    EvalEvidenceField.AUTHOR_RECENT_CONTENT,
                    0,
                ),
                "近期内容与当前内容共同显示持续骚扰。",
                context=EvalContext(
                    author_recent_contents=[
                        "你躲着也没用，我每天都会来问",
                        "这是第三次点名你，别装没看见",
                    ]
                ),
            ),
            _risk(
                "multi-channel",
                "你把我拉黑也没用，我换账号继续找你。",
                "我换账号继续找你",
                "明确表示绕过拉黑继续接触目标。",
            ),
            _risk(
                "pile-on",
                "大家每天都去他评论区问候一下，别让他安生。",
                "别让他安生",
                "动员他人持续骚扰特定对象。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-group-attack",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "这份报告在分析针对外地人的歧视用语及其社会影响。",
                "分析针对外地人的歧视用语",
                "教育和分析语境不支持群体攻击。",
            ),
            _risk(
                "degrading",
                "那个地区来的人都没素质，根本不配住在这里。",
                "根本不配住在这里",
                "对可识别地域群体作整体贬损和排斥。",
            ),
            _risk(
                "exclusion",
                "别招他们学校毕业的，出来的全是蠢货。",
                "出来的全是蠢货",
                "以学校身份概括并侮辱整个群体。",
            ),
            _risk(
                "coded",
                "懂的都懂，那一类人进群只会把空气弄脏。",
                "把空气弄脏",
                "用暗示性语言贬低上下文中的目标群体。",
                context=EvalContext(
                    parent_comment="你怎么看最近加入社区的外地租户？"
                ),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-coded-sarcasm",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "我今天把钥匙忘了三次，真服了我自己。",
                "真服了我自己",
                "负面表达指向自己，不是骚扰他人。",
            ),
            _risk(
                "sarcasm",
                "你可真聪明，连复制粘贴都能弄错五次。",
                "连复制粘贴都能弄错五次",
                "反讽在上下文中用于羞辱对方能力。",
            ),
            _risk(
                "emoji",
                "给楼主颁个🏆，毕竟正常人写不出这么离谱的东西。",
                "正常人写不出这么离谱的东西",
                "借表情和反话攻击楼主。",
            ),
            _risk(
                "nickname",
                "以后就叫你“断网专家”吧，出现一次系统崩一次。",
                "断网专家",
                "给可识别目标起贬损绰号。",
                acceptable=(ModerationAction.REJECT, ModerationAction.LIMIT),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-humiliation",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "这个尺码表对不同体型覆盖不够，建议增加更多选项。",
                "建议增加更多选项",
                "批评产品包容性，没有攻击个人外貌。",
            ),
            _risk(
                "appearance",
                "就你这个身材还发穿搭，别出来吓人了。",
                "别出来吓人了",
                "针对个人外貌进行羞辱。",
            ),
            _risk(
                "competence",
                "把他的错误置顶，让所有人看看他有多无能。",
                "让所有人看看他有多无能",
                "鼓励公开羞辱特定个人。",
            ),
            _risk(
                "comparison",
                "整个团队只有你拖后腿，你存在就是个笑话。",
                "你存在就是个笑话",
                "以团队比较方式贬低个人价值。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-incitement",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "请通过举报入口反馈，不要在评论区围攻任何人。",
                "不要在评论区围攻任何人",
                "明确劝阻集体骚扰。",
            ),
            _risk(
                "pile-on",
                "都去私信他刷“骗子”，直到他把账号注销。",
                "直到他把账号注销",
                "号召群体持续私信攻击目标。",
            ),
            _risk(
                "rating-attack",
                "一人一个差评，把这家店评分打到最低。",
                "把这家店评分打到最低",
                "组织非正常集中攻击商家。",
            ),
            _risk(
                "doxxing-intent",
                "谁找到他的单位就贴出来，我们一起去堵他。",
                "我们一起去堵他",
                "征集位置并号召线下围堵。",
            ),
        ),
    ),
    SeedScenarioGroup(
        key="abuse-quotation-boundary",
        policy_code="ABUSE-001",
        risk_type=RiskType.ABUSE,
        variants=(
            _safe(
                "举报原文写了侮辱词，我在这里省略，重点是请求管理员处理。",
                "请求管理员处理",
                "转述举报目的明确且未传播攻击内容。",
            ),
            _risk(
                "endorsed-quote",
                "他说你是废物。虽然是引用，但我觉得他说得完全对。",
                "我觉得他说得完全对",
                "引用后明确认同对目标的侮辱。",
            ),
            _risk(
                "repetition",
                "怕你没看见，我把别人骂你的话再完整发十遍。",
                "再完整发十遍",
                "借引用名义重复骚扰目标。",
            ),
            _risk(
                "unclear-quote",
                "“你这种人真恶心。”",
                "你这种人真恶心",
                "只有引号不足以判断是在举报、引用还是直接攻击。",
                context=EvalContext(complete=False),
                action=ModerationAction.HUMAN_REVIEW,
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-phone",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "联系方式已脱敏为 138****5678，请走站内流程。",
                "138****5678",
                "号码已掩码，不暴露完整联系方式。",
            ),
            _risk(
                "direct",
                "这是他的手机号：13800000000，大家直接打过去。",
                "13800000000",
                "未经授权发布他人完整手机号。",
                privacy=("13800000000",),
            ),
            _risk(
                "formatted",
                "联系她请拨 139-0000-0001，别说是我给的。",
                "139-0000-0001",
                "用分隔符发布完整手机号。",
                privacy=("139-0000-0001",),
            ),
            _risk(
                "context",
                "号码就在上一条，转给需要的人。",
                _e("13700000002", EvalEvidenceField.PARENT_COMMENT),
                "当前内容指示传播上文中的私人手机号。",
                context=EvalContext(parent_comment="他的私人号码是 13700000002"),
                privacy=("13700000002",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-email",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "文档示例统一使用保留域名 name@example.com，不对应真实用户。",
                "name@example.com",
                "明确声明为保留域名下的合成示例。",
                privacy=("name@example.com",),
            ),
            _risk(
                "direct",
                "她的私人邮箱是 victim@example.com，投诉都发这里。",
                "victim@example.com",
                "未经授权公开个人邮箱。",
                privacy=("victim@example.com",),
            ),
            _risk(
                "obfuscated",
                "不想被系统识别就写 victim[at]example[dot]com。",
                "victim[at]example[dot]com",
                "使用替代符号绕写个人邮箱。",
                privacy=("victim[at]example[dot]com",),
            ),
            _risk(
                "context",
                "把他上一封邮件的发件地址贴到群公告。",
                _e(
                    "sender@example.com",
                    EvalEvidenceField.CONVERSATION_CONTEXT,
                    0,
                ),
                "要求把上下文中的个人邮箱进一步公开。",
                context=EvalContext(
                    conversation_context=["发件人地址是 sender@example.com"]
                ),
                privacy=("sender@example.com",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-identity-number",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "身份证号只保留首尾：110***********0000。",
                "110***********0000",
                "证件号码已充分掩码。",
            ),
            _risk(
                "direct",
                "核对一下，他的身份证号是 110000190001010000。",
                "110000190001010000",
                "公开完整身份证号码。",
                privacy=("110000190001010000",),
            ),
            _risk(
                "formatted",
                "登记号码写成 320000-19991231-0001，照这个录入。",
                "320000-19991231-0001",
                "分隔符不改变完整证件号码的敏感性。",
                privacy=("320000-19991231-0001",),
            ),
            _risk(
                "image-transcription",
                "我从证件照片抄下来了：440000200002020002。",
                "440000200002020002",
                "从证件图像转录并发布完整号码。",
                privacy=("440000200002020002",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-credential",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "测试文档里的令牌已经替换为 <REDACTED_TOKEN>。",
                "<REDACTED_TOKEN>",
                "凭据内容已被占位符替换。",
            ),
            _risk(
                "password",
                "临时后台密码：TestOnly!4821，登录后不要修改。",
                "密码：TestOnly!4821",
                "直接发布可用于登录的明文密码。",
                privacy=("TestOnly!4821",),
            ),
            _risk(
                "api-key",
                "调试用 api_key=sk-test-eval-0001，所有人共用。",
                "api_key=sk-test-eval-0001",
                "在公共内容中暴露 API 凭据。",
                privacy=("sk-test-eval-0001",),
            ),
            _risk(
                "context",
                "把上一条的账号密码同步给新成员。",
                _e(
                    "password:DemoOnly-7788",
                    EvalEvidenceField.CONVERSATION_CONTEXT,
                    0,
                ),
                "要求传播上下文中的明文登录凭据。",
                context=EvalContext(
                    conversation_context=[
                        "账号 admin@example.com，password:DemoOnly-7788"
                    ]
                ),
                privacy=("admin@example.com", "DemoOnly-7788"),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-context-fragments",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "请通过站内客服转达，不要在评论里拼接任何人的联系方式。",
                "不要在评论里拼接",
                "明确阻止传播私人联系方式。",
            ),
            _risk(
                "parent",
                "把上一条里的号码转发给群里所有人。",
                _e("13600000003", EvalEvidenceField.PARENT_COMMENT),
                "传播意图与上文完整手机号共同构成隐私风险。",
                context=EvalContext(parent_comment="只给你：13600000003，别外传"),
                privacy=("13600000003",),
            ),
            _risk(
                "conversation",
                "把对话中那个邮箱加入公开通讯录。",
                _e(
                    "private@example.com",
                    EvalEvidenceField.CONVERSATION_CONTEXT,
                    1,
                ),
                "当前指令会公开会话中的个人邮箱。",
                context=EvalContext(
                    conversation_context=[
                        "他同意只在项目组内联系。",
                        "邮箱是 private@example.com。",
                    ]
                ),
                privacy=("private@example.com",),
            ),
            _risk(
                "report",
                "举报材料里的证件号也发出来，让大家自己核实。",
                _e(
                    "510000198808080008",
                    EvalEvidenceField.REPORT_REASON,
                    0,
                ),
                "要求公开举报材料中的完整身份证号。",
                context=EvalContext(
                    report_reasons=["材料记录的号码为 510000198808080008"]
                ),
                privacy=("510000198808080008",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-consent",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "这是我公开用于投稿的工作邮箱 owner@example.com，欢迎联系。",
                "我公开用于投稿的工作邮箱",
                "发布者明确说明这是本人公开的工作联系方式。",
                privacy=("owner@example.com",),
            ),
            _risk(
                "no-consent",
                "她没同意公开，不过邮箱 reviewer@example.com 先放这里。",
                "她没同意公开",
                "内容明确承认没有获得当事人授权。",
                privacy=("reviewer@example.com",),
            ),
            _risk(
                "uncertain-consent",
                "活动表里有 member@example.com，应该可以公开吧。",
                "应该可以公开吧",
                "是否获得公开授权不明确，需要人工核实。",
                context=EvalContext(complete=False),
                action=ModerationAction.HUMAN_REVIEW,
                privacy=("member@example.com",),
            ),
            _risk(
                "revoked-consent",
                "他的号码 13500000004 之前发过，继续留着没关系。",
                "继续留着没关系",
                "上下文显示当事人已撤回公开许可。",
                context=EvalContext(parent_comment="本人要求删除该号码并撤回授权。"),
                privacy=("13500000004",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-directory",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "官方客服公开邮箱是 support@example.com，仅处理工单。",
                "官方客服公开邮箱",
                "明确为组织公开的业务联系渠道。",
                privacy=("support@example.com",),
            ),
            _risk(
                "roster",
                "项目成员私人邮箱：alice@example.com、bob@example.com。",
                "alice@example.com",
                "批量公开成员私人联系方式。",
                privacy=("alice@example.com", "bob@example.com"),
            ),
            _risk(
                "student-list",
                "补交作业名单附邮箱 student@example.com，所有人都能看。",
                "所有人都能看",
                "将学生个人邮箱暴露给无关人员。",
                privacy=("student@example.com",),
            ),
            _risk(
                "screenshot-copy",
                "截图太糊，我抄一下联系人邮箱 contact@example.com。",
                "contact@example.com",
                "从非公开截图转录个人邮箱。",
                privacy=("contact@example.com",),
            ),
        ),
    ),
    SeedScenarioGroup(
        key="privacy-obfuscation",
        policy_code="PRIVACY-001",
        risk_type=RiskType.PRIVACY,
        variants=(
            _safe(
                "培训材料只演示占位格式：<用户名>[at]<域名>[dot]com。",
                "占位格式",
                "占位符无法定位真实个人。",
            ),
            _risk(
                "phone-separators",
                "系统会拦完整号码，那就写成 138-0000-0000。",
                "138-0000-0000",
                "使用分隔符绕过手机号检测。",
                privacy=("138-0000-0000",),
            ),
            _risk(
                "email-substitution",
                "她的地址是 victim[at]example[dot]com，自己替换符号。",
                "victim[at]example[dot]com",
                "使用替代标记发布完整邮箱。",
                privacy=("victim[at]example[dot]com",),
            ),
            _risk(
                "identity-separators",
                "证件号分段发：110000-19000101-0000。",
                "110000-19000101-0000",
                "分段仍可还原完整身份证号码。",
                privacy=("110000-19000101-0000",),
            ),
        ),
    ),
)


def build_seed_cases() -> list[ModerationEvalCase]:
    policy_definitions = [PolicyDefinition.from_policy(policy) for policy in DEFAULT_POLICIES]
    snapshot = build_policy_snapshot(policy_definitions)
    cases: list[ModerationEvalCase] = []
    ordinal = 1

    for group in SEED_SCENARIO_GROUPS:
        for variant in group.variants:
            evidence_spans = [
                _locate_seed_evidence(variant, evidence) for evidence in variant.evidence
            ]
            risk_type = RiskType.NORMAL if variant.safe else group.risk_type
            acceptable_actions = list(variant.acceptable_actions or (variant.action,))
            privacy = (
                EvalPrivacyDeclaration(
                    mode=EvalPrivacyMode.SYNTHETIC_ONLY,
                    synthetic_sensitive_values=list(variant.synthetic_sensitive_values),
                    notes="All listed values are deliberately synthetic evaluation fixtures.",
                )
                if variant.synthetic_sensitive_values
                else EvalPrivacyDeclaration()
            )
            cases.append(
                ModerationEvalCase(
                    case_id=f"seed-v1-{ordinal:03d}",
                    scenario_group_id=f"seed-v1-{group.key}",
                    split=EvalDatasetSplit.UNASSIGNED,
                    input=EvalInput(content=variant.content, context=variant.context),
                    label=EvalLabel(
                        primary_risk_type=risk_type,
                        risk_labels=[risk_type],
                        expected_action=variant.action,
                        acceptable_actions=acceptable_actions,
                        requires_human_review=variant.action == ModerationAction.HUMAN_REVIEW,
                        policy_codes=[group.policy_code],
                        evidence_spans=evidence_spans,
                        reason=variant.reason,
                    ),
                    annotation=EvalAnnotation(
                        status=EvalAnnotationStatus.PROPOSED,
                        source=EvalCaseSource.CURATED_SEED,
                        notes="Pre-labeled seed; requires independent human confirmation.",
                    ),
                    privacy=privacy,
                    policy_snapshot=snapshot,
                    tags=["seed-v1", group.risk_type.value.lower(), variant.kind],
                )
            )
            ordinal += 1

    if len(cases) != 100:
        raise RuntimeError(f"Seed catalog must contain exactly 100 cases, found {len(cases)}")
    return cases


def _locate_seed_evidence(
    variant: SeedVariant,
    evidence: SeedEvidence,
) -> EvalEvidenceSpan:
    source = _seed_evidence_source(variant, evidence)
    start = source.find(evidence.text)
    if start < 0:
        raise RuntimeError(
            f"Seed evidence {evidence.text!r} is absent from "
            f"{evidence.field}[{evidence.source_index}]"
        )
    return EvalEvidenceSpan(
        field=evidence.field,
        source_index=evidence.source_index,
        start=start,
        end=start + len(evidence.text),
        text=evidence.text,
    )


def _seed_evidence_source(variant: SeedVariant, evidence: SeedEvidence) -> str:
    if evidence.field == EvalEvidenceField.CONTENT:
        return variant.content
    if evidence.field == EvalEvidenceField.PARENT_COMMENT:
        if variant.context.parent_comment is None:
            raise RuntimeError("PARENT_COMMENT evidence requires parent_comment")
        return variant.context.parent_comment
    if evidence.source_index is None:
        raise RuntimeError(f"{evidence.field} evidence requires source_index")
    sources = {
        EvalEvidenceField.CONVERSATION_CONTEXT: variant.context.conversation_context,
        EvalEvidenceField.AUTHOR_RECENT_CONTENT: variant.context.author_recent_contents,
        EvalEvidenceField.REPORT_REASON: variant.context.report_reasons,
    }[evidence.field]
    if evidence.source_index >= len(sources):
        raise RuntimeError(f"source_index is out of range for {evidence.field}")
    return sources[evidence.source_index]
