"""
多Agent深度研究系统 - 核心Agent系统（v2.0 升级版）
包含：SenseNovaClient、WebSearchTool、ResearchOrchestrator、五个核心Agent
Agent架构：Planner → Researcher × N → Writer → Reviewer → Writer(修改) → 完成
"""

import json
import re
import time
import uuid
import hashlib
import requests
from datetime import datetime


# ============================================================
# 工具函数
# ============================================================

def now_ts():
    return datetime.now().isoformat()


def make_log(step, agent, action, input_summary="", output_summary="",
             tool_calls=None, duration_ms=0):
    """构造日志条目"""
    return {
        "step": step,
        "agent": agent,
        "action": action,
        "input_summary": input_summary[:200],
        "output_summary": output_summary[:200],
        "tool_calls": tool_calls or [],
        "duration_ms": duration_ms,
        "timestamp": now_ts(),
    }


def truncate(text, length=200):
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= length else text[:length] + "..."


def extract_json_from_text(text):
    """从文本中提取JSON，处理Markdown代码块等格式"""
    if not text:
        return {}
    content = text.strip()
    # 去掉可能的markdown代码块标记
    if content.startswith("```"):
        content = content.strip("`")
        if content.startswith("json"):
            content = content[4:]
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        # 尝试提取第一个 {...} 块
        match = re.search(r'\{[\s\S]*\}', content)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return {}


# ============================================================
# SenseNova API 客户端
# ============================================================

class SenseNovaClient:
    """SenseNova API 简化客户端"""

    BASE_URL = "https://token.sensenova.cn/v1/chat/completions"
    DEFAULT_MODEL = "sensenova-6.7-flash-lite"

    def __init__(self, api_key, model=None):
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL

    def chat(self, messages, **kwargs):
        """
        单次对话调用
        返回: {content, tool_calls, duration_ms}
        """
        start = time.time()
        payload = {
            "model": kwargs.get("model", self.model),
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2000),
            "stream": False,
        }
        if "tools" in kwargs and kwargs["tools"]:
            payload["tools"] = kwargs["tools"]
            payload["tool_choice"] = kwargs.get("tool_choice", "auto")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            resp = requests.post(self.BASE_URL, json=payload, headers=headers, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            duration_ms = int((time.time() - start) * 1000)

            choice = data.get("choices", [{}])[0]
            message = choice.get("message", {})
            content = message.get("content", "")
            tool_calls = message.get("tool_calls", [])

            return {
                "content": content or "",
                "tool_calls": tool_calls or [],
                "duration_ms": duration_ms,
            }
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            raise RuntimeError(f"SenseNova API 调用失败: {e}") from e

    def chat_with_tools(self, messages, tools, tool_executor, max_iter=3):
        """
        带工具调用的多轮对话
        tool_executor: 函数 (name, arguments_str) -> result_str
        返回: {content, tool_calls_history, duration_ms}
        """
        tool_calls_history = []
        current_messages = list(messages)

        for i in range(max_iter):
            result = self.chat(current_messages, tools=tools)
            tool_calls = result.get("tool_calls", [])

            if not tool_calls:
                # 没有工具调用，返回最终结果
                return {
                    "content": result["content"],
                    "tool_calls_history": tool_calls_history,
                    "duration_ms": result["duration_ms"],
                }

            # 执行工具调用
            current_messages.append({
                "role": "assistant",
                "content": result.get("content", ""),
                "tool_calls": tool_calls,
            })

            for tc in tool_calls:
                tc_id = tc.get("id", "")
                tc_name = tc.get("function", {}).get("name", "")
                tc_args = tc.get("function", {}).get("arguments", "{}")

                try:
                    tool_result = tool_executor(tc_name, tc_args)
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)}, ensure_ascii=False)

                tool_calls_history.append({
                    "name": tc_name,
                    "arguments": tc_args,
                    "result_summary": truncate(tool_result, 200),
                })

                current_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": tool_result,
                })

        # 达到最大迭代次数，返回最后一次内容
        return {
            "content": result.get("content", ""),
            "tool_calls_history": tool_calls_history,
            "duration_ms": result.get("duration_ms", 0),
        }


# ============================================================
# WebSearchTool - 智能模拟搜索工具
# ============================================================

class WebSearchTool:
    """
    智能模拟搜索工具
    预定义商汤科技、科大讯飞等公司的知识库，返回结构化搜索结果
    """

    # 知识库：公司 -> 类别 -> 内容列表
    KNOWLEDGE_BASE = {
        "商汤科技": {
            "公司概况": [
                {"title": "商汤科技 - 公司简介", "snippet": "商汤科技（SenseTime）是一家总部位于中国香港的人工智能公司，由汤晓鸥教授于2014年创立。公司专注于计算机视觉和深度学习技术，是全球领先的人工智能平台公司之一。商汤科技于2021年12月在香港联交所主板上市，股票代码0020.HK。"},
                {"title": "商汤科技发展历程", "snippet": "2014年于香港成立；2017年发布SenseFace人脸平台；2018年进入日本、新加坡市场；2020年发布SenseCore AI大装置；2021年港交所上市；2023年发布日日新SenseNova大模型体系；2024年发布商量大语言模型。"},
                {"title": "商汤科技组织架构", "snippet": "商汤科技总部位于香港，在北京、上海、深圳、杭州、成都等城市设有分部。公司员工约4000人，其中研发人员占比超过70%。创始人汤晓鸥担任董事长，徐立担任CEO。"},
            ],
            "财务表现": [
                {"title": "商汤科技2024年财报", "snippet": "2024年上半年，商汤科技实现营业收入约18.5亿元人民币，同比增长约15%。净亏损收窄至约12亿元，上年同期亏损约20亿元。毛利率提升至48%，主要得益于大模型相关业务的增长。"},
                {"title": "商汤科技营收结构", "snippet": "商汤科技营收主要来自四大板块：智慧商业（占比约35%）、智慧城市（占比约30%）、智慧生活（占比约20%）、智能汽车（占比约15%）。2024年大模型相关收入同比增长超过200%。"},
                {"title": "商汤科技现金储备", "snippet": "截至2024年6月30日，商汤科技现金及现金等价物约95亿元人民币，加上短期投资合计约150亿元，财务状况稳健。研发投入持续维持在营收的60%以上。"},
            ],
            "业务分析": [
                {"title": "商汤科技智慧商业业务", "snippet": "商汤科技智慧商业板块主要面向企业客户提供AI解决方案，包括SenseFoundry方舟城市级视觉开放平台、SenseMARS火星混合现实平台等。2024年企业客户数超过3000家，覆盖金融、零售、地产等多个行业。"},
                {"title": "商汤科技智能汽车业务", "snippet": "商汤科技智能汽车业务（绝影）提供智能驾驶和智能座舱解决方案。截至2024年，已与超过30家车企合作，搭载车型超过100款，累计出货量超过500万辆。"},
                {"title": "商汤日日新大模型", "snippet": "商汤日日新SenseNova大模型体系包括：商量大语言模型、秒画AI文生图、如影数字人、琼宇与格物3D内容生成、星辰AI代码助手等。2024年大模型API调用量日均超过10亿次。"},
            ],
            "竞争格局": [
                {"title": "中国AI公司竞争格局", "snippet": "国内AI市场主要参与者包括：百度（文心一言）、阿里（通义千问）、腾讯（混元）、字节跳动（豆包）、商汤科技（日日新/商量）、科大讯飞（星火）、智谱AI（GLM）、MiniMax等。商汤在计算机视觉领域处于领先地位。"},
                {"title": "商汤科技竞争优势", "snippet": "商汤科技核心竞争力包括：1) 长期积累的计算机视觉技术优势；2) SenseCore AI大装置提供的算力基础设施优势；3) 多行业落地经验和数据积累；4) 自研大模型体系的全栈能力。"},
                {"title": "商汤科技面临的挑战", "snippet": "商汤科技面临的主要挑战：1) 大模型领域互联网巨头的激烈竞争；2) 持续亏损带来的盈利压力；3) 海外市场的地缘政治风险；4) AI行业监管政策的不确定性。"},
            ],
            "技术实力": [
                {"title": "商汤科技技术积累", "snippet": "商汤科技在全球顶级计算机视觉会议（CVPR、ICCV、ECCV）上累计发表论文超过800篇，获得竞赛冠军超过70项。专利申请量超过12000件，其中发明专利占比超过90%。"},
                {"title": "商汤SenseCore AI大装置", "snippet": "SenseCore是商汤科技自研的AI基础设施，包含算力层、平台层和算法层。目前已建成超过20000张GPU的算力集群，可支持万亿参数大模型训练。"},
                {"title": "商汤多模态技术", "snippet": "商汤科技在多模态大模型方面布局全面，涵盖文本、图像、视频、3D、数字人等多个模态。商量大语言模型支持中文理解和生成能力达到国内先进水平。"},
            ],
            "发展战略": [
                {"title": "商汤科技战略方向", "snippet": "商汤科技未来战略聚焦三大方向：1) 大模型基础设施建设，持续升级日日新大模型体系；2) 行业垂直解决方案，深耕金融、医疗、汽车等重点行业；3) 全球化布局，拓展东南亚和中东市场。"},
                {"title": "商汤科技生态建设", "snippet": "商汤科技积极构建AI生态，已联合超过500家合作伙伴。推出AI开发者社区，注册开发者超过100万人。投资孵化了多家AI创业公司。"},
                {"title": "商汤科技长期愿景", "snippet": "商汤科技的愿景是'坚持原创，让AI引领人类进步'。公司目标是成为全球领先的人工智能平台公司，通过技术创新推动各行业的智能化转型。"},
            ],
        },
        "科大讯飞": {
            "公司概况": [
                {"title": "科大讯飞 - 公司简介", "snippet": "科大讯飞股份有限公司（iFLYTEK）成立于1999年，总部位于安徽合肥，是中国智能语音与人工智能领域的龙头企业。公司于2008年在深交所上市，股票代码002230。刘庆峰担任董事长。"},
                {"title": "科大讯飞发展历程", "snippet": "1999年从中科大实验室起步；2008年深交所上市；2010年发布讯飞语音云；2016年发布讯飞超脑计划；2022年发布星火大模型V1.0；2024年星火大模型迭代至V4.0版本。"},
                {"title": "科大讯飞组织架构", "snippet": "科大讯飞总部位于合肥，员工超过16000人，研发人员占比约60%。公司在全国设有多个研发中心，业务覆盖教育、医疗、政务、消费者等多个领域。"},
            ],
            "财务表现": [
                {"title": "科大讯飞2024年财报", "snippet": "2024年上半年，科大讯飞实现营业收入约105亿元人民币，同比增长约20%。归母净利润约5亿元，同比增长约30%。毛利率维持在40%左右。经营性现金流明显改善。"},
                {"title": "科大讯飞营收结构", "snippet": "科大讯飞营收主要来自：教育领域（占比约30%）、智慧城市（占比约25%）、消费者业务（占比约20%）、医疗健康（占比约10%）、汽车业务（占比约8%）、其他（约7%）。"},
                {"title": "科大讯飞盈利能力", "snippet": "科大讯飞2023年研发投入约35亿元，占营收比例约18%。公司已实现持续盈利，2023年归母净利润约7.8亿元。大模型相关业务成为新的增长引擎。"},
            ],
            "业务分析": [
                {"title": "科大讯飞教育业务", "snippet": "科大讯飞教育业务是核心支柱，产品涵盖智慧课堂、智慧考试、智慧校园、个性化学习等。服务全国超过5万所学校，用户超过1亿。AI学习机系列产品市场占有率领先。"},
                {"title": "科大讯飞星火大模型", "snippet": "讯飞星火大模型是科大讯飞的核心产品，截至2024年已迭代至V4.0版本，在中文理解、逻辑推理、代码生成等方面达到国内领先水平。星火大模型已在教育、医疗、办公等多个场景落地。"},
                {"title": "科大讯飞消费者业务", "snippet": "科大讯飞消费者业务包括：讯飞翻译机、讯飞录音笔、讯飞学习机、讯飞办公本等AI硬件产品。翻译机系列在全球市场占有率领先，录音笔国内市场份额超过50%。"},
            ],
            "竞争格局": [
                {"title": "智能语音市场竞争格局", "snippet": "中国智能语音市场主要参与者：科大讯飞（市场份额约60%，稳居第一）、百度、阿里云、腾讯云、思必驰、云知声等。科大讯飞在语音识别准确率、方言支持等方面保持领先。"},
                {"title": "科大讯飞竞争优势", "snippet": "科大讯飞核心竞争力：1) 语音技术深厚积累，20多年专注；2) 教育等垂直行业深度落地；3) 海量行业数据积累；4) 产学研一体化优势（中科大背景）；5) 国家队背景的资源优势。"},
                {"title": "科大讯飞面临挑战", "snippet": "科大讯飞面临的挑战：1) 大模型领域互联网巨头竞争；2) 教育业务政策风险；3) 消费者产品品牌力不足；4) 高端人才竞争加剧；5) 海外市场拓展缓慢。"},
            ],
            "技术实力": [
                {"title": "科大讯飞技术积累", "snippet": "科大讯飞在语音识别、语音合成、自然语言理解等领域拥有深厚技术积累。多次在国际语音合成大赛（Blizzard Challenge）、机器翻译大赛中获得冠军。拥有专利超过5000件。"},
                {"title": "科大讯飞星火大模型技术", "snippet": "讯飞星火大模型采用自研的训练框架，支持多模态输入输出。在中文领域优势明显，支持多种方言理解。模型训练基于国产算力平台，具有自主可控能力。"},
                {"title": "科大讯飞AI+医疗技术", "snippet": "科大讯飞在医疗AI领域布局深入，智医助理已通过国家执业医师资格考试，在基层医疗辅助诊断方面广泛应用。覆盖全国超过300个区县，服务基层医生超过50万人。"},
            ],
            "发展战略": [
                {"title": "科大讯飞战略方向", "snippet": "科大讯飞未来战略：1) 持续迭代星火大模型，保持技术领先；2) 深化'AI+行业'战略，聚焦教育、医疗、汽车等重点赛道；3) 推动To C业务增长，打造爆款消费级产品；4) 布局海外市场。"},
                {"title": "科大讯飞生态布局", "snippet": "科大讯飞开放平台已聚集超过500万开发者，AI应用数量超过100万。建立了讯飞AI生态产业基金，投资了数十家AI创业公司。与多所高校建立联合实验室。"},
                {"title": "科大讯飞长期目标", "snippet": "科大讯飞的目标是成为全球领先的人工智能公司，实现'人工智能服务每个行业、每个家庭、每个人'的愿景。公司计划到2030年成为千亿级营收企业。"},
            ],
        },
    }

    # 通用搜索结果（当公司不在知识库中时使用）
    GENERIC_RESULTS = {
        "公司概况": [
            {"title": "{company} 公司概况", "snippet": "{company}是一家在行业内具有一定影响力的企业。公司成立多年，在主营业务领域积累了丰富的经验和客户资源。公司总部位于主要城市，在全国多个地区设有分支机构。"},
            {"title": "{company} 发展历程", "snippet": "{company}自成立以来经历了多个发展阶段。从初创期的业务探索，到成长期的快速扩张，再到成熟期的多元化布局，公司逐步发展壮大。近年来积极推进数字化转型。"},
        ],
        "财务表现": [
            {"title": "{company} 财务状况", "snippet": "{company}近年来整体经营状况平稳。营业收入保持稳定增长，毛利率维持在行业平均水平。公司持续加大研发投入，以提升长期竞争力。现金流状况良好，资产负债率处于合理区间。"},
            {"title": "{company} 业绩分析", "snippet": "{company}的营收结构呈现多元化特征，主营业务贡献主要收入来源。公司盈利能力受行业周期和市场竞争影响有所波动，但整体保持在合理水平。"},
        ],
        "业务分析": [
            {"title": "{company} 主营业务", "snippet": "{company}的主营业务涵盖多个领域，核心业务在行业内具有较强的竞争力。公司不断优化业务结构，聚焦高增长赛道，同时积极培育新的业务增长点。"},
            {"title": "{company} 产品与服务", "snippet": "{company}提供的产品和服务在市场上获得了客户的广泛认可。公司注重产品创新和服务质量，持续迭代升级产品体系，以满足客户不断变化的需求。"},
        ],
        "竞争格局": [
            {"title": "{company} 行业竞争格局", "snippet": "{company}所处的行业竞争较为激烈，市场参与者众多。公司在细分领域具有一定的竞争优势，但也面临头部企业和新兴竞争者的双重挑战。"},
            {"title": "{company} 竞争优劣势分析", "snippet": "{company}的竞争优势主要体现在技术积累、客户资源和品牌影响力等方面。劣势可能包括创新能力不足、市场反应速度较慢等。公司需要持续提升核心竞争力。"},
        ],
        "技术实力": [
            {"title": "{company} 技术研发", "snippet": "{company}高度重视技术研发，每年投入大量资源用于技术创新。公司拥有一支专业的研发团队，在核心技术领域积累了一定的专利和知识产权。"},
            {"title": "{company} 数字化转型", "snippet": "{company}积极推进数字化转型，运用大数据、人工智能等新技术提升运营效率和客户体验。公司建立了数字化平台，推动业务流程的智能化升级。"},
        ],
        "发展战略": [
            {"title": "{company} 战略规划", "snippet": "{company}未来发展战略聚焦核心业务增长和新兴业务培育。公司计划通过技术创新、市场拓展和生态建设等方式，实现高质量可持续发展。"},
            {"title": "{company} 未来展望", "snippet": "{company}对未来发展持乐观态度。公司将继续深耕主业，同时积极把握行业变革带来的机遇，努力实现新一轮的增长和突破。"},
        ],
    }

    TOOL_SCHEMA = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索网络信息，获取公司相关的最新资讯、财务数据、业务信息等",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词",
                    },
                },
                "required": ["query"],
            },
        },
    }

    def __init__(self):
        self.search_count = 0

    def search(self, query, company_name):
        """
        模拟搜索，返回结构化JSON结果
        """
        self.search_count += 1
        time.sleep(0.3)  # 模拟网络延迟

        # 判断最相关的类别
        category = self._match_category(query)

        # 获取知识库结果
        results = self._get_results(company_name, category)

        # 根据query微调相关性排序
        scored = []
        for r in results:
            score = 0
            text = r["title"] + r["snippet"]
            for word in query.split():
                if word in text:
                    score += 1
            scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        sorted_results = [r for _, r in scored]

        output = {
            "query": query,
            "company": company_name,
            "total": len(sorted_results),
            "results": sorted_results,
            "search_time": now_ts(),
        }
        return json.dumps(output, ensure_ascii=False)

    def _match_category(self, query):
        """根据查询匹配类别"""
        category_keywords = {
            "公司概况": ["简介", "概况", "介绍", "成立", "历史", "总部", "创始人", "发展历程", "公司背景"],
            "财务表现": ["财务", "营收", "收入", "利润", "亏损", "财报", "业绩", "毛利率", "现金流", "盈利"],
            "业务分析": ["业务", "产品", "服务", "营收结构", "商业模式", "客户", "市场", "销售"],
            "竞争格局": ["竞争", "对手", "市场份额", "格局", "优势", "劣势", "竞品", "竞争对手"],
            "技术实力": ["技术", "研发", "专利", "算法", "模型", "创新", "科研", "论文"],
            "发展战略": ["战略", "规划", "愿景", "目标", "未来", "布局", "生态", "方向"],
        }
        best_cat = "公司概况"
        best_score = 0
        for cat, keywords in category_keywords.items():
            score = sum(1 for kw in keywords if kw in query)
            if score > best_score:
                best_score = score
                best_cat = cat
        return best_cat

    def _get_results(self, company_name, category):
        """获取搜索结果"""
        # 精确匹配公司名
        kb_company = None
        for name in self.KNOWLEDGE_BASE:
            if name in company_name or company_name in name:
                kb_company = name
                break

        if kb_company and category in self.KNOWLEDGE_BASE[kb_company]:
            return list(self.KNOWLEDGE_BASE[kb_company][category])

        # 使用通用结果
        results = []
        for item in self.GENERIC_RESULTS.get(category, []):
            results.append({
                "title": item["title"].format(company=company_name),
                "snippet": item["snippet"].format(company=company_name),
            })
        return results


# ============================================================
# 公司类型与维度模板（动态维度规划）
# ============================================================

# 公司类型及其对应的研究维度模板
COMPANY_TYPE_DIMENSIONS = {
    "科技": [
        {"name": "公司概况", "description": "公司基本信息、发展历程、组织架构",
         "key_questions": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"],
         "search_keywords": ["公司简介", "发展历程", "创始人", "组织架构"]},
        {"name": "财务表现", "description": "营收、利润、毛利率、现金流等财务指标",
         "key_questions": ["公司最近的财务状况如何？", "营收结构和盈利能力怎样？"],
         "search_keywords": ["财务报表", "营收", "净利润", "毛利率"]},
        {"name": "业务分析", "description": "主营业务、产品线、商业模式",
         "key_questions": ["公司的主营业务和产品有哪些？", "公司的商业模式和客户群体是什么？"],
         "search_keywords": ["产品线", "商业模式", "客户案例", "营收结构"]},
        {"name": "技术实力", "description": "研发投入、专利、核心技术、技术团队",
         "key_questions": ["公司的技术研发实力如何？", "公司有哪些核心技术和专利？"],
         "search_keywords": ["技术专利", "研发投入", "核心算法", "技术团队"]},
        {"name": "竞争格局", "description": "行业地位、竞争对手、市场份额",
         "key_questions": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
         "search_keywords": ["市场份额", "竞争对手", "行业排名", "竞争优势"]},
        {"name": "发展战略", "description": "战略规划、未来布局、增长动力",
         "key_questions": ["公司的发展战略和规划是什么？", "公司未来的增长动力在哪里？"],
         "search_keywords": ["战略规划", "未来布局", "增长策略", "新业务"]},
        {"name": "产品与创新", "description": "核心产品、创新能力、产品迭代",
         "key_questions": ["公司的核心产品有哪些创新？", "产品迭代和创新节奏如何？"],
         "search_keywords": ["产品创新", "新功能", "产品迭代", "创新能力"]},
        {"name": "人才与组织", "description": "员工规模、人才结构、组织文化",
         "key_questions": ["公司的人才结构和规模如何？", "组织文化和人才战略是什么？"],
         "search_keywords": ["员工数量", "人才战略", "组织文化", "研发人员占比"]},
    ],
    "制造": [
        {"name": "公司概况", "description": "公司基本信息、发展历程、产能布局",
         "key_questions": ["公司的基本情况和发展历程是什么？", "公司的产能布局和生产基地如何？"],
         "search_keywords": ["公司简介", "发展历程", "生产基地", "产能"]},
        {"name": "财务表现", "description": "营收、利润、成本结构、资产负债",
         "key_questions": ["公司最近的财务状况如何？", "成本结构和盈利能力怎样？"],
         "search_keywords": ["财务报表", "营收", "净利润", "资产负债率"]},
        {"name": "业务分析", "description": "产品线、产能、供应链、客户",
         "key_questions": ["公司的主要产品和产能如何？", "供应链管理和客户结构怎样？"],
         "search_keywords": ["产品线", "产能", "供应链", "主要客户"]},
        {"name": "竞争格局", "description": "行业地位、竞争对手、市场份额",
         "key_questions": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
         "search_keywords": ["市场份额", "竞争对手", "行业排名", "竞争优势"]},
        {"name": "技术实力", "description": "研发投入、核心技术、工艺水平",
         "key_questions": ["公司的技术研发实力如何？", "生产工艺和技术水平怎样？"],
         "search_keywords": ["技术专利", "研发投入", "生产工艺", "核心技术"]},
        {"name": "发展战略", "description": "产能扩张、产品升级、国际化",
         "key_questions": ["公司的发展战略和规划是什么？", "产能扩张和国际化布局如何？"],
         "search_keywords": ["战略规划", "产能扩张", "国际化", "产品升级"]},
        {"name": "供应链与质量", "description": "供应链管理、质量控制、上游资源",
         "key_questions": ["公司的供应链管理能力如何？", "质量控制体系怎样？"],
         "search_keywords": ["供应链", "质量控制", "上游供应商", "质量管理"]},
    ],
    "消费": [
        {"name": "公司概况", "description": "公司基本信息、品牌历史、发展历程",
         "key_questions": ["公司的基本情况和发展历程是什么？", "品牌定位和发展历史如何？"],
         "search_keywords": ["公司简介", "品牌历史", "发展历程", "创始人"]},
        {"name": "财务表现", "description": "营收、利润、毛利率、销售费用",
         "key_questions": ["公司最近的财务状况如何？", "毛利率和费用结构怎样？"],
         "search_keywords": ["财务报表", "营收", "净利润", "毛利率"]},
        {"name": "业务分析", "description": "产品线、品牌矩阵、渠道布局",
         "key_questions": ["公司的产品和品牌矩阵如何？", "销售渠道和市场覆盖怎样？"],
         "search_keywords": ["产品线", "品牌矩阵", "销售渠道", "市场覆盖"]},
        {"name": "竞争格局", "description": "市场份额、竞品分析、品牌力",
         "key_questions": ["行业竞争格局如何？", "公司的品牌力和市场地位怎样？"],
         "search_keywords": ["市场份额", "竞争对手", "品牌价值", "行业排名"]},
        {"name": "营销与渠道", "description": "营销策略、渠道管理、用户增长",
         "key_questions": ["公司的营销策略是什么？", "渠道管理和用户增长情况如何？"],
         "search_keywords": ["营销策略", "渠道管理", "用户增长", "品牌营销"]},
        {"name": "发展战略", "description": "品牌升级、品类拓展、国际化",
         "key_questions": ["公司的发展战略和规划是什么？", "品类拓展和国际化布局如何？"],
         "search_keywords": ["战略规划", "品类拓展", "国际化", "品牌升级"]},
        {"name": "消费者洞察", "description": "用户画像、满意度、复购率",
         "key_questions": ["公司的核心用户画像是什么？", "用户满意度和复购率如何？"],
         "search_keywords": ["用户画像", "消费者调研", "满意度", "复购率"]},
    ],
    "金融": [
        {"name": "公司概况", "description": "公司基本信息、牌照资质、发展历程",
         "key_questions": ["公司的基本情况和发展历程是什么？", "公司拥有哪些金融牌照？"],
         "search_keywords": ["公司简介", "发展历程", "金融牌照", "资质"]},
        {"name": "财务表现", "description": "营收、利润、资产规模、风控指标",
         "key_questions": ["公司最近的财务状况如何？", "资产质量和风控指标怎样？"],
         "search_keywords": ["财务报表", "营收", "净利润", "不良率"]},
        {"name": "业务分析", "description": "业务结构、产品线、客户结构",
         "key_questions": ["公司的主要业务和产品有哪些？", "客户结构和业务占比如何？"],
         "search_keywords": ["业务结构", "金融产品", "客户结构", "业务占比"]},
        {"name": "竞争格局", "description": "行业地位、竞争对手、市场份额",
         "key_questions": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
         "search_keywords": ["市场份额", "竞争对手", "行业排名", "竞争优势"]},
        {"name": "风险管理", "description": "风控体系、不良率、合规管理",
         "key_questions": ["公司的风险管理体系如何？", "不良率和合规情况怎样？"],
         "search_keywords": ["风险管理", "不良率", "合规", "风控体系"]},
        {"name": "发展战略", "description": "业务转型、科技赋能、国际化",
         "key_questions": ["公司的发展战略和规划是什么？", "金融科技和数字化转型如何？"],
         "search_keywords": ["战略规划", "金融科技", "数字化转型", "业务转型"]},
        {"name": "监管与合规", "description": "监管环境、合规体系、政策影响",
         "key_questions": ["公司面临的监管环境如何？", "合规体系和政策影响怎样？"],
         "search_keywords": ["监管政策", "合规体系", "金融监管", "政策影响"]},
    ],
    "医疗": [
        {"name": "公司概况", "description": "公司基本信息、发展历程、资质认证",
         "key_questions": ["公司的基本情况和发展历程是什么？", "公司有哪些资质认证？"],
         "search_keywords": ["公司简介", "发展历程", "资质认证", "医疗器械"]},
        {"name": "财务表现", "description": "营收、利润、研发投入、毛利率",
         "key_questions": ["公司最近的财务状况如何？", "研发投入和毛利率怎样？"],
         "search_keywords": ["财务报表", "营收", "净利润", "研发投入"]},
        {"name": "业务分析", "description": "产品线、适应症、销售渠道",
         "key_questions": ["公司的主要产品和适应症有哪些？", "销售渠道和市场覆盖如何？"],
         "search_keywords": ["产品线", "适应症", "销售渠道", "市场覆盖"]},
        {"name": "竞争格局", "description": "市场份额、竞品管线、行业地位",
         "key_questions": ["行业竞争格局如何？", "公司的市场地位和竞争优势怎样？"],
         "search_keywords": ["市场份额", "竞争对手", "行业排名", "竞争优势"]},
        {"name": "研发管线", "description": "在研产品、临床试验、技术平台",
         "key_questions": ["公司的研发管线有哪些？", "临床试验进展如何？"],
         "search_keywords": ["研发管线", "临床试验", "在研产品", "技术平台"]},
        {"name": "发展战略", "description": "产品拓展、国际化、BD合作",
         "key_questions": ["公司的发展战略和规划是什么？", "国际化和BD合作进展如何？"],
         "search_keywords": ["战略规划", "国际化", "BD合作", "产品拓展"]},
        {"name": "监管与审批", "description": "药品审批、监管政策、合规情况",
         "key_questions": ["公司产品的审批进展如何？", "监管政策影响怎样？"],
         "search_keywords": ["药品审批", "NMPA", "FDA", "监管政策"]},
    ],
}

# 默认维度模板（通用）
DEFAULT_DIMENSIONS = [
    {"name": "公司概况", "description": "公司基本信息、发展历程、组织架构",
     "key_questions": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"],
     "search_keywords": ["公司简介", "发展历程", "组织架构"]},
    {"name": "财务表现", "description": "营收、利润、毛利率等财务指标",
     "key_questions": ["公司最近的财务状况如何？", "营收结构和盈利能力怎样？"],
     "search_keywords": ["财务报表", "营收", "净利润"]},
    {"name": "业务分析", "description": "主营业务、产品线、商业模式",
     "key_questions": ["公司的主营业务和产品有哪些？", "公司的商业模式和客户群体是什么？"],
     "search_keywords": ["主营业务", "产品线", "商业模式"]},
    {"name": "竞争格局", "description": "行业地位、竞争对手、市场份额",
     "key_questions": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
     "search_keywords": ["市场份额", "竞争对手", "竞争优势"]},
    {"name": "技术实力", "description": "研发投入、核心技术、创新能力",
     "key_questions": ["公司的技术研发实力如何？", "公司有哪些核心技术和专利？"],
     "search_keywords": ["技术专利", "研发投入", "核心技术"]},
    {"name": "发展战略", "description": "战略规划、未来布局、增长动力",
     "key_questions": ["公司的发展战略和规划是什么？", "公司未来的增长动力在哪里？"],
     "search_keywords": ["战略规划", "未来布局", "增长策略"]},
    {"name": "风险与挑战", "description": "主要风险、面临挑战、应对策略",
     "key_questions": ["公司面临的主要风险和挑战是什么？", "公司的应对策略如何？"],
     "search_keywords": ["风险因素", "行业挑战", "应对策略"]},
]

# 不同深度对应的维度数量
DEPTH_DIMENSION_COUNT = {
    "basic": 3,
    "standard": 5,
    "deep": 7,
}


# ============================================================
# Planner Agent - 动态维度规划
# ============================================================

PLANNER_SYSTEM_PROMPT = """你是资深研究总监，负责制定公司研究计划。
你的任务是根据公司名称和研究深度，制定一份结构化的研究计划。

第一步：先做公司画像，判断公司类型（科技/制造/消费/金融/医疗/其他）
第二步：根据公司类型动态生成研究维度

研究计划应该包含多个研究维度，每个维度包含名称、描述、2-3个关键问题、以及搜索关键词。
请以严格的JSON格式输出，不要包含任何其他文字。

输出格式示例：
{
  "plan_name": "XX公司深度研究计划",
  "company_profile": {
    "company_type": "科技",
    "type_reason": "该公司主要从事AI技术研发，属于科技行业",
    "industry": "人工智能",
    "scale": "中大型"
  },
  "dimensions": [
    {
      "name": "公司概况",
      "description": "公司基本信息、发展历程、组织架构",
      "key_questions": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"],
      "search_keywords": ["公司简介", "发展历程", "创始人", "组织架构"]
    }
  ]
}
"""


def _get_dimensions_for_type(company_type, depth):
    """根据公司类型和深度获取维度列表"""
    templates = COMPANY_TYPE_DIMENSIONS.get(company_type, DEFAULT_DIMENSIONS)
    count = DEPTH_DIMENSION_COUNT.get(depth, 3)
    # 取前count个维度，确保不超过模板数量
    return templates[:min(count, len(templates))]


def _generate_fallback_plan(company_name, depth):
    """生成保底研究计划"""
    # 使用通用维度模板
    dimensions = _get_dimensions_for_type("通用", depth)
    # 生成保底的公司画像
    company_profile = {
        "company_type": "其他",
        "type_reason": "默认类型，需要进一步确认",
        "industry": "未知",
        "scale": "未知",
    }
    return {
        "plan_name": f"{company_name}深度研究计划",
        "company_profile": company_profile,
        "dimensions": [
            {
                "name": d["name"],
                "description": d["description"],
                "key_questions": d["key_questions"],
                "search_keywords": d["search_keywords"],
            }
            for d in dimensions
        ],
    }


def planner_agent(client, company_name, depth):
    """
    规划阶段 Agent（升级版：公司画像 + 动态维度）
    返回: {"plan": plan_dict, "duration_ms": int, input_summary, output_summary}
    """
    dimension_count = DEPTH_DIMENSION_COUNT.get(depth, 3)

    user_prompt = f"""请为「{company_name}」制定一份深度研究计划。

研究深度：{depth}
维度数量：{dimension_count}个维度

请按以下步骤进行：
1. 先做公司画像：判断公司类型（从科技/制造/消费/金融/医疗中选择最匹配的）
2. 根据公司类型和研究深度，生成{dimension_count}个最相关的研究维度
3. 每个维度包含：名称、描述、2-3个关键问题、3-5个搜索关键词

请严格以JSON格式输出研究计划。"""

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.4, max_tokens=2500)

    # 解析JSON
    plan = extract_json_from_text(result["content"])

    # 验证plan结构是否完整
    if not plan or "dimensions" not in plan or not plan["dimensions"]:
        plan = _generate_fallback_plan(company_name, depth)

    # 确保company_profile存在
    if "company_profile" not in plan:
        plan["company_profile"] = {
            "company_type": "其他",
            "type_reason": "自动推断",
            "industry": "未知",
            "scale": "未知",
        }

    # 确保每个维度都有完整字段
    for dim in plan["dimensions"]:
        if "description" not in dim:
            dim["description"] = f"{dim.get('name', '')}相关分析"
        if "search_keywords" not in dim:
            dim["search_keywords"] = [dim.get("name", "")]
        if "key_questions" not in dim:
            dim["key_questions"] = [f"{dim.get('name', '')}的核心信息是什么？"]

    # 确保维度数量符合深度要求
    expected_count = DEPTH_DIMENSION_COUNT.get(depth, 3)
    if len(plan["dimensions"]) > expected_count:
        plan["dimensions"] = plan["dimensions"][:expected_count]

    return {
        "plan": plan,
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 深度:{depth}, 预期维度数:{dimension_count}",
        "output_summary": truncate(json.dumps(plan, ensure_ascii=False), 200),
    }


# ============================================================
# Researcher Agent
# ============================================================

RESEARCHER_SYSTEM_PROMPT = """你是资深行业研究员，负责对公司进行深度调研。
你的任务是针对给定的研究维度和关键问题，利用web_search工具收集信息，
并整理出结构化的调研结果。

请遵循以下原则：
1. 先调用web_search工具搜索相关信息
2. 根据搜索结果整理调研内容
3. 确保信息准确、全面、有条理
4. 引用信息时注明来源

请以JSON格式输出结果，格式如下：
{
  "dimension": "维度名称",
  "summary": "维度的整体总结（200-300字）",
  "key_findings": [
    "关键发现1",
    "关键发现2",
    "关键发现3"
  ],
  "data_points": [
    {"metric": "指标名称", "value": "数值", "source": "来源标题"}
  ],
  "sources": [
    {"title": "来源标题", "relevance": "high/medium/low"}
  ]
}
"""


def researcher_agent(client, search_tool, company_name, dimension_name, key_questions,
                     search_keywords=None, depth="basic"):
    """
    调研阶段 Agent（单个维度）
    返回: {"result": result_dict, "tool_calls_history": [...], "duration_ms": int}
    """
    questions_str = "\n".join([f"- {q}" for q in key_questions])
    keywords_str = ""
    if search_keywords:
        keywords_str = f"\n建议搜索关键词：{', '.join(search_keywords)}"

    user_prompt = f"""请对「{company_name}」进行「{dimension_name}」维度的调研。

需要回答的关键问题：
{questions_str}
{keywords_str}

请先使用web_search工具搜索相关信息，然后整理出结构化的调研结果。
搜索关键词应该围绕上述关键问题设计。"""

    messages = [
        {"role": "system", "content": RESEARCHER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    def tool_executor(name, arguments_str):
        if name == "web_search":
            try:
                args = json.loads(arguments_str)
                query = args.get("query", "")
            except json.JSONDecodeError:
                query = arguments_str
            return search_tool.search(query, company_name)
        return json.dumps({"error": f"Unknown tool: {name}"}, ensure_ascii=False)

    # 决定搜索次数（basic: 1次，standard/deep: 2次）
    max_iter = 2 if depth in ("standard", "deep") else 1

    result = client.chat_with_tools(
        messages,
        tools=[WebSearchTool.TOOL_SCHEMA],
        tool_executor=tool_executor,
        max_iter=max_iter + 1,  # +1 用于最终总结
    )

    # 解析结果
    research_result = extract_json_from_text(result["content"])

    if not research_result or "dimension" not in research_result:
        # 保底：直接用文本内容
        research_result = {
            "dimension": dimension_name,
            "summary": result["content"][:300] if result["content"] else "",
            "key_findings": [f"基于搜索结果的{dimension_name}分析"],
            "data_points": [],
            "sources": [],
        }

    # 确保data_points存在
    if "data_points" not in research_result:
        research_result["data_points"] = []

    return {
        "result": research_result,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"维度:{dimension_name}, 问题数:{len(key_questions)}",
        "output_summary": truncate(json.dumps(research_result, ensure_ascii=False), 200),
    }


# ============================================================
# Writer Agent - 支持审核修改 + 三层输出
# ============================================================

WRITER_SYSTEM_PROMPT = """你是资深行业研究报告撰写专家。
你的任务是根据各维度的调研结果，撰写一份高质量的公司深度研究报告。

报告要求：
1. 结构清晰，层次分明
2. 内容全面，数据详实
3. 分析深入，观点明确
4. 语言专业，表达流畅

请输出完整的Markdown格式报告。
"""

WRITER_REVISION_PROMPT = """你是资深行业研究报告撰写专家。
你的任务是根据审核意见对研究报告进行修改和完善。

请认真阅读审核意见，针对每个问题进行修改：
1. 数据准确性：补充缺失的数据来源，修正不准确的数据
2. 逻辑一致性：调整前后矛盾的论述
3. 结构完整性：补充缺失的章节
4. 可读性：优化语言表达，使报告更通顺

修改时请保持报告的整体风格和结构，只修改有问题的部分。
请输出修改后的完整Markdown报告。
"""


def writer_agent(client, company_name, research_results, depth, revision=False,
                 review_opinions=None, original_report=None):
    """
    报告撰写 Agent（升级版：支持审核修改 + 三层输出）
    参数:
        revision: 是否为修改模式
        review_opinions: 审核意见（修改模式下提供）
        original_report: 原始报告（修改模式下提供）
    返回: {"report": str, "duration_ms": int, input_summary, output_summary}
    """
    if revision and original_report and review_opinions:
        # 修改模式：根据审核意见修改报告
        opinions_text = json.dumps(review_opinions, ensure_ascii=False, indent=2)
        user_prompt = f"""请根据以下审核意见，修改「{company_name}」的研究报告。

【审核意见】
{opinions_text}

【原始报告】
{original_report}

请输出修改后的完整Markdown报告。确保：
1. 逐条回应并修改审核意见中指出的问题
2. 保持报告整体结构和风格
3. 修改后输出完整报告，不要只输出修改部分"""

        messages = [
            {"role": "system", "content": WRITER_REVISION_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        result = client.chat(messages, temperature=0.5, max_tokens=4000)
        report = result["content"].strip()

        return {
            "report": report,
            "duration_ms": result["duration_ms"],
            "input_summary": f"修改模式, 公司:{company_name}, 审核意见数:{len(review_opinions.get('issues', []))}",
            "output_summary": truncate(report, 200),
        }

    # 首次撰写模式
    # 构建调研结果摘要
    results_text = ""
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        summary = res.get("summary", "")
        findings = res.get("key_findings", [])
        findings_str = "\n".join([f"- {f}" for f in findings])
        data_points = res.get("data_points", [])
        data_str = ""
        if data_points:
            data_items = [f"- {dp.get('metric', '')}: {dp.get('value', '')}" for dp in data_points[:5]]
            data_str = "\n**关键数据**：\n" + "\n".join(data_items)
        results_text += f"""
## 维度{i}：{dim_name}

**概要**：{summary}

**关键发现**：
{findings_str}
{data_str}
"""

    user_prompt = f"""请根据以下调研结果，为「{company_name}」撰写一份深度研究报告。

研究深度：{depth}
调研维度：{len(research_results)}个

---

{results_text}

---

请撰写一份结构完整、内容详实的Markdown格式研究报告。报告应包含：
1. 封面/标题
2. 研究摘要
3. 各维度详细分析（对应调研维度）
4. 总结与展望

请直接输出Markdown内容，不要有其他说明。"""

    messages = [
        {"role": "system", "content": WRITER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.5, max_tokens=4000)

    report = result["content"].strip()

    return {
        "report": report,
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 维度数:{len(research_results)}, 深度:{depth}",
        "output_summary": truncate(report, 200),
    }


# ============================================================
# Reviewer Agent - 质量审核
# ============================================================

REVIEWER_SYSTEM_PROMPT = """你是资深研究报告质量审核专家。
你的任务是对公司研究报告进行全面质量审核，从多个维度评估报告质量，
并给出具体的修改建议。

审核维度：
1. 数据准确性：关键数据是否有来源支撑，数据是否准确可靠
2. 逻辑一致性：前后论述是否一致，推理是否合理
3. 结构完整性：章节是否齐全，结构是否清晰
4. 可读性：语言是否通顺，表达是否专业

请以严格的JSON格式输出审核结果，不要包含任何其他文字。

输出格式：
{
  "overall_score": 85,
  "dimension_scores": {
    "data_accuracy": 80,
    "logical_consistency": 85,
    "structural_completeness": 90,
    "readability": 85
  },
  "issues": [
    {
      "type": "data_accuracy",
      "severity": "high/medium/low",
      "location": "章节位置",
      "description": "具体问题描述",
      "suggestion": "修改建议"
    }
  ],
  "summary": "审核总结，概述报告的主要优点和需要改进的方面"
}
"""


def reviewer_agent(client, company_name, report, research_results, depth):
    """
    质量审核 Agent
    返回: {"review": review_dict, "duration_ms": int, input_summary, output_summary}
    """
    # 构建调研结果摘要（用于审核时对比信息来源）
    results_summary = ""
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        findings = res.get("key_findings", [])
        sources = res.get("sources", [])
        findings_str = "; ".join(findings[:3])
        sources_str = "; ".join([s.get("title", "") for s in sources[:3]])
        results_summary += f"- {dim_name}: {findings_str} [来源: {sources_str}]\n"

    user_prompt = f"""请对「{company_name}」的研究报告进行质量审核。

【报告内容】
{report}

【调研结果摘要（用于验证数据来源）】
{results_summary}

【审核要求】
请从以下四个维度进行审核：
1. 数据准确性（data_accuracy）：关键数据是否有来源支撑，数据是否准确
2. 逻辑一致性（logical_consistency）：前后论述是否一致，推理是否合理
3. 结构完整性（structural_completeness）：章节是否齐全，结构是否清晰
4. 可读性（readability）：语言是否通顺，表达是否专业

请给出总体评分（0-100分）和各维度评分，列出具体问题和修改建议。
研究深度为：{depth}

请严格以JSON格式输出审核结果。"""

    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.3, max_tokens=2500)

    # 解析审核结果
    review = extract_json_from_text(result["content"])

    # 保底审核结果
    if not review or "overall_score" not in review:
        review = {
            "overall_score": 70,
            "dimension_scores": {
                "data_accuracy": 70,
                "logical_consistency": 75,
                "structural_completeness": 75,
                "readability": 75,
            },
            "issues": [
                {
                    "type": "data_accuracy",
                    "severity": "medium",
                    "location": "全文",
                    "description": "部分数据未明确标注来源",
                    "suggestion": "建议在关键数据处补充数据来源说明",
                },
            ],
            "summary": "报告整体结构完整，但数据来源标注可以进一步完善。",
        }

    # 确保issues字段存在
    if "issues" not in review:
        review["issues"] = []

    return {
        "review": review,
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 报告长度:{len(report)}字符",
        "output_summary": f"总分:{review.get('overall_score', 0)}, 问题数:{len(review.get('issues', []))}",
    }


# ============================================================
# Fact Check - 事实核查
# ============================================================

FACT_CHECK_PROMPT = """你是事实核查专家。
你的任务是从研究报告中抽取所有带数字的关键断言，
并对其可信度进行评估。

请重点关注以下类型的数据：
- 营收、利润、增长率等财务数据
- 市场份额、行业排名
- 用户数、客户数、员工数
- 专利数、论文数等技术指标
- 产品出货量、覆盖率等业务数据

请以严格的JSON格式输出核查结果。

输出格式：
{
  "total_claims": 10,
  "fact_checks": [
    {
      "claim": "报告中的原始断言",
      "metric_type": "营收/利润/增长率/市场份额/其他",
      "value": "提取的数值",
      "confidence": "high/medium/low",
      "confidence_reason": "可信度判断依据，如：多个来源交叉验证/单一来源/无明确来源",
      "location": "所在章节"
    }
  ],
  "overall_data_credibility": "high/medium/low",
  "summary": "数据可信度总体评价"
}
"""


def _extract_numeric_claims_simple(report):
    """简单的数字断言提取（保底方案）"""
    # 匹配包含数字的句子
    sentences = re.split(r'[。！？\n]', report)
    claims = []
    # 关键指标关键词
    metric_keywords = {
        "营收": "营收", "收入": "营收", "营收收入": "营收",
        "利润": "利润", "净利润": "利润", "净亏损": "利润",
        "增长率": "增长率", "同比增长": "增长率",
        "毛利率": "毛利率",
        "市场份额": "市场份额", "占有率": "市场份额",
        "员工": "员工数", "人员": "员工数",
        "专利": "专利数",
        "用户": "用户数", "客户": "客户数",
        "出货量": "出货量",
        "研发投入": "研发投入",
    }

    for sent in sentences:
        sent = sent.strip()
        if not sent:
            continue
        # 检查是否包含数字
        if not re.search(r'\d+(\.\d+)?', sent):
            continue
        # 检查是否包含关键指标关键词
        metric_type = "其他"
        for kw, mtype in metric_keywords.items():
            if kw in sent:
                metric_type = mtype
                break
        if metric_type == "其他" and len(claims) >= 15:
            continue
        # 提取数值
        numbers = re.findall(r'\d+(\.\d+)?[%万亿亿元人月天年篇家款辆]?', sent)
        value = numbers[0] if numbers else "未知"

        claims.append({
            "claim": sent[:100],
            "metric_type": metric_type,
            "value": value,
            "confidence": "medium",
            "confidence_reason": "基于调研结果推断，需进一步验证",
            "location": "正文中",
        })

    return claims[:20]


def fact_check(client, company_name, report, research_results, depth):
    """
    事实核查：抽取报告中的数字断言并评估可信度
    返回: {"fact_check_result": dict, "duration_ms": int, input_summary, output_summary}
    """
    # basic深度简化处理
    if depth == "basic":
        claims = _extract_numeric_claims_simple(report)
        # basic深度只取前8个
        claims = claims[:8]
        # 简单评估可信度（基于是否有data_points支撑）
        all_data_points = []
        for res in research_results:
            all_data_points.extend(res.get("data_points", []))

        for claim in claims:
            # 检查research_results中是否有对应数据
            has_source = any(
                dp.get("metric", "") and dp.get("metric", "")[:2] in claim["claim"]
                for dp in all_data_points
            )
            if has_source:
                claim["confidence"] = "high"
                claim["confidence_reason"] = "有调研数据来源支撑"
            else:
                claim["confidence"] = "medium"
                claim["confidence_reason"] = "基于调研结果推断，单一来源"

        high_count = sum(1 for c in claims if c["confidence"] == "high")
        overall = "high" if high_count >= len(claims) * 0.7 else (
            "medium" if high_count >= len(claims) * 0.4 else "low"
        ) if claims else "medium"

        result = {
            "total_claims": len(claims),
            "fact_checks": claims,
            "overall_data_credibility": overall,
            "summary": f"共核查{len(claims)}条数据断言，整体可信度为{overall}。",
        }

        return {
            "fact_check_result": result,
            "duration_ms": 0,
            "input_summary": f"公司:{company_name}, 模式:basic简化版",
            "output_summary": f"核查{len(claims)}条, 整体可信度:{overall}",
        }

    # standard/deep深度：使用LLM进行事实核查
    # 构建调研数据点摘要
    data_points_summary = ""
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        data_points = res.get("data_points", [])
        sources = res.get("sources", [])
        if data_points:
            dp_str = "; ".join([f"{dp.get('metric', '')}={dp.get('value', '')}" for dp in data_points[:5]])
            src_str = "; ".join([s.get("title", "") for s in sources[:3]])
            data_points_summary += f"- {dim_name}: {dp_str} [来源: {src_str}]\n"

    user_prompt = f"""请对「{company_name}」的研究报告进行事实核查。

【报告内容】
{report[:8000]}

【调研数据点参考】
{data_points_summary}

【核查要求】
1. 抽取报告中所有带数字的关键断言（重点：营收、利润、增长率、市场份额、员工数、专利数等）
2. 结合调研数据点评估每条断言的可信度（high/medium/low）
3. 可信度依据：是否有多个来源交叉验证、来源是否权威
4. 标注每条断言所在的章节位置

研究深度：{depth}

请严格以JSON格式输出核查结果。"""

    messages = [
        {"role": "system", "content": FACT_CHECK_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.2, max_tokens=2500)

    # 解析结果
    fc_result = extract_json_from_text(result["content"])

    if not fc_result or "fact_checks" not in fc_result:
        # 保底
        claims = _extract_numeric_claims_simple(report)
        fc_result = {
            "total_claims": len(claims),
            "fact_checks": claims,
            "overall_data_credibility": "medium",
            "summary": "基于规则提取的数据断言，整体可信度中等。",
        }

    return {
        "fact_check_result": fc_result,
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 深度:{depth}",
        "output_summary": f"核查{fc_result.get('total_claims', 0)}条, 整体可信度:{fc_result.get('overall_data_credibility', 'unknown')}",
    }


# ============================================================
# 三层输出结构生成
# ============================================================

def generate_three_layer_output(client, company_name, report, research_results, fact_check_result, depth):
    """
    生成三层输出结构：executive_summary + key_metrics + full_report
    返回: {"executive_summary": str, "key_metrics": dict, "full_report": str, "duration_ms": int}
    """
    start = time.time()

    # full_report就是最终报告
    full_report = report

    # 使用LLM生成执行摘要和核心指标
    system_prompt = """你是研究报告总结专家。请从完整的研究报告中提取：
1. 执行摘要（3-5句话，概括核心结论）
2. 核心指标（8-10个结构化数据点）

请以严格的JSON格式输出。

输出格式：
{
  "executive_summary": "3-5句话的执行摘要...",
  "key_metrics": {
    "company_name": "公司名称",
    "revenue": "营收数据",
    "profit": "利润数据",
    "gross_margin": "毛利率",
    "employee_count": "员工数量",
    "patent_count": "专利数量",
    "market_share": "市场份额",
    "growth_rate": "增长率",
    "rd_ratio": "研发投入占比",
    "founded_year": "成立年份"
  }
}
"""

    user_prompt = f"""请从「{company_name}」的研究报告中提取执行摘要和核心指标。

【报告内容】
{report[:6000]}

请输出：
1. executive_summary: 3-5句话的执行摘要，概括公司核心情况和主要结论
2. key_metrics: 8-10个核心指标的结构化数据，包括但不限于：公司名、营收、利润、毛利率、员工数、专利数、市场份额、增长率等

请严格以JSON格式输出。"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.3, max_tokens=1500)

    parsed = extract_json_from_text(result["content"])

    executive_summary = parsed.get("executive_summary", "")
    key_metrics = parsed.get("key_metrics", {})

    # 如果提取失败，生成保底内容
    if not executive_summary:
        executive_summary = f"{company_name}是行业内的重要企业，在主营业务领域具有较强竞争力。公司财务状况整体稳健，营收保持稳定增长。公司积极布局未来战略，持续加大研发投入，以应对市场竞争和行业变革。"

    if not key_metrics:
        key_metrics = {
            "company_name": company_name,
            "revenue": "数据待补充",
            "profit": "数据待补充",
            "gross_margin": "数据待补充",
            "employee_count": "数据待补充",
            "patent_count": "数据待补充",
            "market_share": "数据待补充",
            "growth_rate": "数据待补充",
        }

    # 将事实核查结果附加到key_metrics中
    key_metrics["data_credibility"] = fact_check_result.get("overall_data_credibility", "medium")

    duration_ms = int((time.time() - start) * 1000)

    return {
        "executive_summary": executive_summary,
        "key_metrics": key_metrics,
        "full_report": full_report,
        "duration_ms": duration_ms,
    }


# ============================================================
# ResearchOrchestrator - 研究编排器（升级版）
# ============================================================

class ResearchOrchestrator:
    """研究编排器 - 协调五个Agent完成完整研究流程（v2.0）

    流程：Planner → [Researcher × N] → Writer → Reviewer → Writer(修改) → Fact Check → 完成
    """

    def __init__(self):
        pass

    def run(self, company_name, depth, api_key, event_callback=None):
        """
        执行完整研究流程（升级版）
        event_callback: 函数 (event_type, data_dict) -> None
        返回: {
            "executive_summary": str,
            "key_metrics": dict,
            "full_report": str,
            "report": str,  # 向后兼容
            "logs": [...],
            "research_results": [...],
            "plan": {...},
            "review": {...},
            "fact_check_result": {...},
            "total_duration_ms": int,
            "company_name": str,
            "depth": str,
        }
        """
        logs = []
        step_counter = [0]
        research_results = []

        def emit(event_type, data):
            if event_callback:
                try:
                    event_callback(event_type, data)
                except Exception:
                    pass

        def add_log(agent, action, input_summary="", output_summary="",
                    tool_calls=None, duration_ms=0):
            step_counter[0] += 1
            log = make_log(
                step=step_counter[0],
                agent=agent,
                action=action,
                input_summary=input_summary,
                output_summary=output_summary,
                tool_calls=tool_calls,
                duration_ms=duration_ms,
            )
            logs.append(log)
            emit("log", log)
            return log

        total_start = time.time()

        try:
            # ---- 阶段1：规划（Planner） ----
            emit("progress", {"percent": 5, "phase": "planning", "message": "开始制定研究计划..."})
            emit("agent_start", {"agent": "planner", "phase": "规划阶段"})

            add_log("planner", "启动规划Agent", input_summary=f"公司:{company_name}, 深度:{depth}")

            client = SenseNovaClient(api_key)
            search_tool = WebSearchTool()

            plan_result = planner_agent(client, company_name, depth)

            add_log(
                "planner", "完成研究计划",
                input_summary=plan_result["input_summary"],
                output_summary=plan_result["output_summary"],
                duration_ms=plan_result["duration_ms"],
            )

            plan = plan_result["plan"]
            dimensions = plan.get("dimensions", [])
            company_profile = plan.get("company_profile", {})

            emit("progress", {"percent": 15, "phase": "planning",
                              "message": f"研究计划已制定，共{len(dimensions)}个维度"})
            emit("planner_result", {
                "plan": plan,
                "company_profile": company_profile,
                "dimensions_count": len(dimensions),
            })
            emit("agent_end", {"agent": "planner", "status": "success",
                               "output_summary": plan_result["output_summary"]})

            # ---- 阶段2：调研（Researcher × N） ----
            emit("progress", {"percent": 20, "phase": "researching",
                              "message": "开始调研阶段..."})

            total_dims = len(dimensions)
            for i, dim in enumerate(dimensions):
                dim_name = dim.get("name", f"维度{i+1}")
                key_questions = dim.get("key_questions", [])
                search_keywords = dim.get("search_keywords", [])

                # 计算进度（调研阶段占比：20% → 60%）
                base_progress = 20
                research_range = 40
                dim_progress = base_progress + int((i / total_dims) * research_range)

                emit("progress", {"percent": dim_progress, "phase": "researching",
                                  "message": f"正在调研：{dim_name} ({i+1}/{total_dims})"})
                emit("agent_start", {"agent": "researcher", "phase": f"调研阶段 - {dim_name}",
                                     "dimension": dim_name, "index": i + 1,
                                     "total": total_dims})

                add_log(
                    "researcher", f"开始调研「{dim_name}」",
                    input_summary=f"维度:{dim_name}, 问题:{len(key_questions)}个",
                )

                res_result = researcher_agent(
                    client, search_tool, company_name, dim_name, key_questions,
                    search_keywords=search_keywords, depth=depth
                )

                research_results.append(res_result["result"])

                add_log(
                    "researcher", f"完成「{dim_name}」调研",
                    input_summary=res_result["input_summary"],
                    output_summary=res_result["output_summary"],
                    tool_calls=res_result["tool_calls_history"],
                    duration_ms=res_result["duration_ms"],
                )

                # 每个维度的工具调用也单独发事件
                for tc in res_result["tool_calls_history"]:
                    emit("tool_call", {
                        "agent": "researcher",
                        "dimension": dim_name,
                        "tool": tc["name"],
                        "arguments": tc["arguments"],
                        "result_summary": tc["result_summary"],
                    })

                emit("agent_end", {"agent": "researcher", "status": "success",
                                   "dimension": dim_name,
                                   "output_summary": res_result["output_summary"]})

            # ---- 阶段3：报告撰写（Writer - 初稿） ----
            emit("progress", {"percent": 62, "phase": "writing",
                              "message": "开始撰写研究报告初稿..."})
            emit("agent_start", {"agent": "writer", "phase": "报告撰写阶段（初稿）"})

            add_log(
                "writer", "启动报告撰写Agent（初稿）",
                input_summary=f"维度数:{len(research_results)}",
            )

            writer_result = writer_agent(client, company_name, research_results, depth)
            draft_report = writer_result["report"]

            add_log(
                "writer", "完成报告初稿",
                input_summary=writer_result["input_summary"],
                output_summary=writer_result["output_summary"],
                duration_ms=writer_result["duration_ms"],
            )

            emit("agent_end", {"agent": "writer", "status": "success",
                               "output_summary": writer_result["output_summary"],
                               "stage": "draft"})

            # ---- 阶段4：质量审核（Reviewer） ----
            emit("progress", {"percent": 75, "phase": "reviewing",
                              "message": "开始质量审核..."})
            emit("agent_start", {"agent": "reviewer", "phase": "质量审核阶段"})
            emit("reviewer_start", {"agent": "reviewer", "phase": "质量审核"})

            add_log(
                "reviewer", "启动质量审核Agent",
                input_summary=f"公司:{company_name}, 报告长度:{len(draft_report)}字符",
            )

            reviewer_result = reviewer_agent(client, company_name, draft_report, research_results, depth)
            review = reviewer_result["review"]

            add_log(
                "reviewer", "完成质量审核",
                input_summary=reviewer_result["input_summary"],
                output_summary=reviewer_result["output_summary"],
                duration_ms=reviewer_result["duration_ms"],
            )

            emit("reviewer_end", {
                "agent": "reviewer",
                "status": "success",
                "overall_score": review.get("overall_score", 0),
                "issues_count": len(review.get("issues", [])),
                "output_summary": reviewer_result["output_summary"],
            })
            emit("agent_end", {"agent": "reviewer", "status": "success",
                               "output_summary": reviewer_result["output_summary"]})

            # ---- 阶段5：报告修改（Writer - 终稿） ----
            final_report = draft_report
            overall_score = review.get("overall_score", 0)
            issues = review.get("issues", [])

            # basic深度跳过修改，standard/deep需要修改（迭代1轮）
            need_revision = (depth in ("standard", "deep")) and len(issues) > 0 and overall_score < 90

            if need_revision:
                emit("progress", {"percent": 85, "phase": "revising",
                                  "message": "根据审核意见修改报告..."})
                emit("agent_start", {"agent": "writer", "phase": "报告修改阶段"})

                add_log(
                    "writer", "启动报告修改",
                    input_summary=f"审核问题数:{len(issues)}, 总分:{overall_score}",
                )

                revision_result = writer_agent(
                    client, company_name, research_results, depth,
                    revision=True, review_opinions=review, original_report=draft_report
                )

                final_report = revision_result["report"]

                add_log(
                    "writer", "完成报告修改",
                    input_summary=revision_result["input_summary"],
                    output_summary=revision_result["output_summary"],
                    duration_ms=revision_result["duration_ms"],
                )

                emit("agent_end", {"agent": "writer", "status": "success",
                                   "output_summary": revision_result["output_summary"],
                                   "stage": "final"})
            else:
                add_log(
                    "writer", "跳过报告修改",
                    input_summary=f"深度:{depth}, 总分:{overall_score}, 问题数:{len(issues)}",
                    output_summary="无需修改，直接使用初稿",
                )

            # ---- 阶段6：事实核查（Fact Check） ----
            emit("progress", {"percent": 92, "phase": "fact_check",
                              "message": "进行事实核查..."})
            emit("fact_check", {"phase": "start", "message": "开始事实核查"})

            add_log(
                "fact_check", "启动事实核查",
                input_summary=f"公司:{company_name}, 深度:{depth}",
            )

            fc_result = fact_check(client, company_name, final_report, research_results, depth)
            fact_check_result = fc_result["fact_check_result"]

            add_log(
                "fact_check", "完成事实核查",
                input_summary=fc_result["input_summary"],
                output_summary=fc_result["output_summary"],
                duration_ms=fc_result["duration_ms"],
            )

            emit("fact_check", {
                "phase": "end",
                "total_claims": fact_check_result.get("total_claims", 0),
                "overall_credibility": fact_check_result.get("overall_data_credibility", ""),
                "output_summary": fc_result["output_summary"],
            })

            # ---- 阶段7：生成三层输出 ----
            emit("progress", {"percent": 97, "phase": "finalizing",
                              "message": "生成最终输出..."})

            three_layer = generate_three_layer_output(
                client, company_name, final_report, research_results, fact_check_result, depth
            )

            emit("progress", {"percent": 100, "phase": "complete",
                              "message": "研究完成！"})

            total_duration = int((time.time() - total_start) * 1000)

            result = {
                "executive_summary": three_layer["executive_summary"],
                "key_metrics": three_layer["key_metrics"],
                "full_report": three_layer["full_report"],
                "report": three_layer["full_report"],  # 向后兼容
                "logs": logs,
                "research_results": research_results,
                "plan": plan,
                "review": review,
                "fact_check_result": fact_check_result,
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
            }

            emit("complete", {
                "executive_summary": three_layer["executive_summary"],
                "key_metrics": three_layer["key_metrics"],
                "full_report": three_layer["full_report"],
                "report": three_layer["full_report"],  # 向后兼容
                "logs": logs,
                "total_duration_ms": total_duration,
                "review": review,
                "fact_check_result": fact_check_result,
            })

            return result

        except Exception as e:
            total_duration = int((time.time() - total_start) * 1000)
            error_msg = str(e)

            add_log(
                "system", "研究失败",
                input_summary="",
                output_summary=error_msg,
                duration_ms=total_duration,
            )

            emit("error", {"message": error_msg})
            emit("complete", {
                "executive_summary": f"研究失败：{error_msg}",
                "key_metrics": {"company_name": company_name},
                "full_report": f"# 研究失败\n\n错误信息：{error_msg}",
                "report": f"# 研究失败\n\n错误信息：{error_msg}",
                "logs": logs,
                "total_duration_ms": total_duration,
                "error": error_msg,
            })

            return {
                "executive_summary": f"研究失败：{error_msg}",
                "key_metrics": {"company_name": company_name},
                "full_report": f"# 研究失败\n\n错误信息：{error_msg}",
                "report": f"# 研究失败\n\n错误信息：{error_msg}",
                "logs": logs,
                "research_results": research_results,
                "plan": {},
                "review": {},
                "fact_check_result": {},
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
                "error": error_msg,
            }
