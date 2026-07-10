"""
多Agent深度研究系统 - 核心Agent系统
包含：SenseNovaClient、WebSearchTool、ResearchOrchestrator、三个核心Agent
"""

import json
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
        返回: {content, tool_calls_history}
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
# 三个核心 Agent（函数式设计）
# ============================================================

# --- Planner Agent ---

PLANNER_SYSTEM_PROMPT = """你是资深研究总监，负责制定公司研究计划。
你的任务是根据公司名称和研究深度，制定一份结构化的研究计划。

研究计划应该包含多个研究维度，每个维度包含2-3个关键问题。
请以严格的JSON格式输出，不要包含任何其他文字。

输出格式示例：
{
  "plan_name": "XX公司深度研究计划",
  "dimensions": [
    {
      "name": "公司概况",
      "key_questions": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"]
    },
    {
      "name": "财务表现",
      "key_questions": ["公司最近的财务状况如何？", "营收结构和盈利能力怎样？"]
    }
  ]
}
"""

# 不同深度的维度数量
DEPTH_DIMENSIONS = {
    "basic": ["公司概况", "财务表现"],
    "standard": ["公司概况", "财务表现", "业务分析", "竞争格局"],
    "deep": ["公司概况", "财务表现", "业务分析", "竞争格局", "技术实力", "发展战略"],
}


def planner_agent(client, company_name, depth):
    """
    规划阶段 Agent
    返回: {"plan": plan_dict, "duration_ms": int}
    """
    dimension_list = DEPTH_DIMENSIONS.get(depth, DEPTH_DIMENSIONS["basic"])
    dim_desc = "、".join(dimension_list)

    user_prompt = f"""请为「{company_name}」制定一份深度研究计划。

研究深度：{depth}
研究维度：{dim_desc}（共{len(dimension_list)}个维度）

请为每个维度设计2-3个关键问题，确保研究全面且有深度。
请严格以JSON格式输出研究计划。"""

    messages = [
        {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    result = client.chat(messages, temperature=0.4)

    # 解析JSON
    content = result["content"].strip()
    # 尝试提取JSON
    try:
        # 去掉可能的markdown代码块标记
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        plan = json.loads(content)
    except json.JSONDecodeError:
        # 如果解析失败，生成保底计划
        plan = _generate_fallback_plan(company_name, dimension_list)

    return {
        "plan": plan,
        "duration_ms": result["duration_ms"],
        "input_summary": f"公司:{company_name}, 深度:{depth}, 维度:{dim_desc}",
        "output_summary": truncate(json.dumps(plan, ensure_ascii=False), 200),
    }


def _generate_fallback_plan(company_name, dimension_list):
    """生成保底研究计划"""
    questions_map = {
        "公司概况": ["公司的基本情况和发展历程是什么？", "公司的组织架构和核心团队如何？"],
        "财务表现": ["公司最近的财务状况如何？", "营收结构和盈利能力怎样？"],
        "业务分析": ["公司的主营业务和产品有哪些？", "公司的商业模式和客户群体是什么？"],
        "竞争格局": ["行业竞争格局如何？", "公司的竞争优势和劣势是什么？"],
        "技术实力": ["公司的技术研发实力如何？", "公司有哪些核心技术和专利？"],
        "发展战略": ["公司的发展战略和规划是什么？", "公司未来的增长动力在哪里？"],
    }
    dimensions = []
    for dim in dimension_list:
        dimensions.append({
            "name": dim,
            "key_questions": questions_map.get(dim, ["该维度的核心信息是什么？"]),
        })
    return {
        "plan_name": f"{company_name}深度研究计划",
        "dimensions": dimensions,
    }


# --- Researcher Agent ---

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
  "sources": [
    {"title": "来源标题", "relevance": "high/medium/low"}
  ]
}
"""


def researcher_agent(client, search_tool, company_name, dimension_name, key_questions, depth="basic"):
    """
    调研阶段 Agent（单个维度）
    返回: {"result": result_dict, "tool_calls_history": [...], "duration_ms": int}
    """
    questions_str = "\n".join([f"- {q}" for q in key_questions])

    user_prompt = f"""请对「{company_name}」进行「{dimension_name}」维度的调研。

需要回答的关键问题：
{questions_str}

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
    content = result["content"].strip()
    try:
        if content.startswith("```"):
            content = content.strip("`")
            if content.startswith("json"):
                content = content[4:]
        content = content.strip()
        research_result = json.loads(content)
    except json.JSONDecodeError:
        # 保底：直接用文本内容
        research_result = {
            "dimension": dimension_name,
            "summary": content[:300],
            "key_findings": [f"基于搜索结果的{dimension_name}分析"],
            "sources": [],
        }

    return {
        "result": research_result,
        "tool_calls_history": result["tool_calls_history"],
        "duration_ms": result["duration_ms"],
        "input_summary": f"维度:{dimension_name}, 问题数:{len(key_questions)}",
        "output_summary": truncate(json.dumps(research_result, ensure_ascii=False), 200),
    }


# --- Writer Agent ---

WRITER_SYSTEM_PROMPT = """你是资深行业研究报告撰写专家。
你的任务是根据各维度的调研结果，撰写一份高质量的公司深度研究报告。

报告要求：
1. 结构清晰，层次分明
2. 内容全面，数据详实
3. 分析深入，观点明确
4. 语言专业，表达流畅

请输出完整的Markdown格式报告。
"""


def writer_agent(client, company_name, research_results, depth):
    """
    报告撰写 Agent
    返回: {"report": str, "duration_ms": int}
    """
    # 构建调研结果摘要
    results_text = ""
    for i, res in enumerate(research_results, 1):
        dim_name = res.get("dimension", f"维度{i}")
        summary = res.get("summary", "")
        findings = res.get("key_findings", [])
        findings_str = "\n".join([f"- {f}" for f in findings])
        results_text += f"""
## 维度{i}：{dim_name}

**概要**：{summary}

**关键发现**：
{findings_str}
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
# ResearchOrchestrator - 研究编排器
# ============================================================

class ResearchOrchestrator:
    """研究编排器 - 协调三个Agent完成完整研究流程"""

    def __init__(self):
        pass

    def run(self, company_name, depth, api_key, event_callback=None):
        """
        执行完整研究流程
        event_callback: 函数 (event_type, data_dict) -> None
        返回: {"report": str, "logs": [...], "research_results": [...]}
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
            # ---- 阶段1：规划 ----
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

            emit("progress", {"percent": 15, "phase": "planning",
                              "message": f"研究计划已制定，共{len(dimensions)}个维度"})
            emit("agent_end", {"agent": "planner", "status": "success",
                               "output_summary": plan_result["output_summary"]})

            # ---- 阶段2：调研 ----
            emit("progress", {"percent": 20, "phase": "researching",
                              "message": "开始调研阶段..."})

            total_dims = len(dimensions)
            for i, dim in enumerate(dimensions):
                dim_name = dim.get("name", f"维度{i+1}")
                key_questions = dim.get("key_questions", [])

                # 计算进度
                base_progress = 20
                research_range = 70  # 20% - 90%
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
                    client, search_tool, company_name, dim_name, key_questions, depth
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

            # ---- 阶段3：报告撰写 ----
            emit("progress", {"percent": 90, "phase": "writing",
                              "message": "开始撰写研究报告..."})
            emit("agent_start", {"agent": "writer", "phase": "报告撰写阶段"})

            add_log(
                "writer", "启动报告撰写Agent",
                input_summary=f"维度数:{len(research_results)}",
            )

            writer_result = writer_agent(client, company_name, research_results, depth)

            add_log(
                "writer", "完成报告撰写",
                input_summary=writer_result["input_summary"],
                output_summary=writer_result["output_summary"],
                duration_ms=writer_result["duration_ms"],
            )

            emit("progress", {"percent": 100, "phase": "writing",
                              "message": "研究完成！"})
            emit("agent_end", {"agent": "writer", "status": "success",
                               "output_summary": writer_result["output_summary"]})

            total_duration = int((time.time() - total_start) * 1000)

            result = {
                "report": writer_result["report"],
                "logs": logs,
                "research_results": research_results,
                "plan": plan,
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
            }

            emit("complete", {
                "report": writer_result["report"],
                "logs": logs,
                "total_duration_ms": total_duration,
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
                "report": f"# 研究失败\n\n错误信息：{error_msg}",
                "logs": logs,
                "total_duration_ms": total_duration,
                "error": error_msg,
            })

            return {
                "report": f"# 研究失败\n\n错误信息：{error_msg}",
                "logs": logs,
                "research_results": research_results,
                "plan": {},
                "total_duration_ms": total_duration,
                "company_name": company_name,
                "depth": depth,
                "error": error_msg,
            }
