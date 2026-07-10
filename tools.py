"""
多Agent深度研究系统 - 工具定义模块

包含三个核心工具：
- web_search: 通用网页搜索
- company_lookup: 企业基本信息查询
- financial_data: 财务数据查询

所有工具均为智能模拟实现（基于知识库），但调用流程是真实的 function calling。
工具调用遵循 OpenAI function calling 规范，支持参数校验和错误处理。
"""

import json
import time
import logging
from datetime import datetime
from typing import Dict, List, Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 工具函数
# ============================================================

def now_ts() -> str:
    """获取当前时间的ISO格式字符串"""
    return datetime.now().isoformat()


def truncate(text: str, length: int = 200) -> str:
    """截断文本到指定长度"""
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= length else text[:length] + "..."


# ============================================================
# 知识库定义
# ============================================================

class KnowledgeBase:
    """
    知识库 - 存储公司相关的结构化数据

    包含公司概况、财务数据、业务信息、竞争格局、技术实力、发展战略等维度。
    用于模拟搜索工具和查询工具的数据来源。
    """

    # 详细知识库：公司名 -> 类别 -> 内容列表
    COMPANY_KNOWLEDGE = {
        "商汤科技": {
            "基本信息": {
                "全称": "商汤科技集团有限公司",
                "英文名": "SenseTime",
                "成立时间": "2014年",
                "创始人": "汤晓鸥",
                "董事长": "汤晓鸥",
                "CEO": "徐立",
                "总部": "中国香港",
                "员工数": "约4000人",
                "研发人员占比": "超过70%",
                "上市时间": "2021年12月",
                "上市地点": "香港联交所主板",
                "股票代码": "0020.HK",
                "所属行业": "人工智能 / 计算机视觉",
                "主营业务": "计算机视觉技术、AI大模型、智慧城市、智能汽车、智慧商业、智慧生活",
            },
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
            "财务数据": {
                "2024H1": {
                    "营业收入": "18.5亿元人民币",
                    "同比增长": "15%",
                    "净亏损": "12亿元人民币",
                    "毛利率": "48%",
                    "研发投入": "约11亿元",
                    "研发投入占比": "约60%",
                },
                "2023": {
                    "营业收入": "37.8亿元人民币",
                    "同比增长": "8%",
                    "净亏损": "28.2亿元人民币",
                    "毛利率": "44%",
                },
                "营收结构": {
                    "智慧商业": "35%",
                    "智慧城市": "30%",
                    "智慧生活": "20%",
                    "智能汽车": "15%",
                },
                "现金及等价物": "95亿元人民币（截至2024年6月）",
            },
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
            "基本信息": {
                "全称": "科大讯飞股份有限公司",
                "英文名": "iFLYTEK",
                "成立时间": "1999年",
                "创始人": "刘庆峰等中科大团队",
                "董事长": "刘庆峰",
                "CEO": "刘庆峰",
                "总部": "安徽合肥",
                "员工数": "超过16000人",
                "研发人员占比": "约60%",
                "上市时间": "2008年",
                "上市地点": "深圳证券交易所",
                "股票代码": "002230.SZ",
                "所属行业": "人工智能 / 智能语音",
                "主营业务": "智能语音技术、AI大模型、智慧教育、智慧城市、消费者业务、医疗健康、智能汽车",
            },
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
            "财务数据": {
                "2024H1": {
                    "营业收入": "105亿元人民币",
                    "同比增长": "20%",
                    "归母净利润": "5亿元人民币",
                    "同比净利润增长": "30%",
                    "毛利率": "40%",
                    "研发投入": "约19亿元",
                    "研发投入占比": "约18%",
                },
                "2023": {
                    "营业收入": "194亿元人民币",
                    "同比增长": "12%",
                    "归母净利润": "7.8亿元人民币",
                    "毛利率": "41%",
                },
                "营收结构": {
                    "教育领域": "30%",
                    "智慧城市": "25%",
                    "消费者业务": "20%",
                    "医疗健康": "10%",
                    "汽车业务": "8%",
                    "其他": "7%",
                },
            },
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

    # 通用搜索结果模板（公司不在知识库中时使用）
    GENERIC_TEMPLATES = {
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

    # 类别关键词映射（用于搜索query匹配类别）
    CATEGORY_KEYWORDS = {
        "公司概况": ["简介", "概况", "介绍", "成立", "历史", "总部", "创始人", "发展历程", "公司背景", "组织架构", "员工"],
        "财务表现": ["财务", "营收", "收入", "利润", "亏损", "财报", "业绩", "毛利率", "现金流", "盈利", "净利润", "营收结构"],
        "业务分析": ["业务", "产品", "服务", "营收结构", "商业模式", "客户", "市场", "销售", "主营"],
        "竞争格局": ["竞争", "对手", "市场份额", "格局", "优势", "劣势", "竞品", "竞争对手", "行业地位"],
        "技术实力": ["技术", "研发", "专利", "算法", "模型", "创新", "科研", "论文", "算力"],
        "发展战略": ["战略", "规划", "愿景", "目标", "未来", "布局", "生态", "方向"],
    }

    @classmethod
    def match_company(cls, company_name: str) -> Optional[str]:
        """
        匹配知识库中的公司

        Args:
            company_name: 公司名称

        Returns:
            匹配到的知识库公司名，未匹配返回None
        """
        for name in cls.COMPANY_KNOWLEDGE:
            if name in company_name or company_name in name:
                return name
        return None

    @classmethod
    def match_category(cls, query: str) -> str:
        """
        根据搜索query匹配最相关的类别

        Args:
            query: 搜索关键词

        Returns:
            匹配到的类别名称
        """
        best_cat = "公司概况"
        best_score = 0
        for cat, keywords in cls.CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw in query)
            if score > best_score:
                best_score = score
                best_cat = cat
        return best_cat

    @classmethod
    def get_search_results(cls, company_name: str, category: str) -> List[Dict[str, str]]:
        """
        获取搜索结果列表

        Args:
            company_name: 公司名称
            category: 搜索类别

        Returns:
            搜索结果列表，每项包含title和snippet
        """
        kb_company = cls.match_company(company_name)

        if kb_company and category in cls.COMPANY_KNOWLEDGE[kb_company]:
            # 从详细知识库获取
            results = cls.COMPANY_KNOWLEDGE[kb_company].get(category, [])
            if results:
                return list(results)

        # 使用通用模板
        results = []
        for item in cls.GENERIC_TEMPLATES.get(category, []):
            results.append({
                "title": item["title"].format(company=company_name),
                "snippet": item["snippet"].format(company=company_name),
            })
        return results

    @classmethod
    def get_company_info(cls, company_name: str) -> Dict[str, Any]:
        """
        获取公司基本信息

        Args:
            company_name: 公司名称

        Returns:
            公司基本信息字典
        """
        kb_company = cls.match_company(company_name)

        if kb_company and "基本信息" in cls.COMPANY_KNOWLEDGE[kb_company]:
            return dict(cls.COMPANY_KNOWLEDGE[kb_company]["基本信息"])

        # 返回通用基本信息
        return {
            "公司名称": company_name,
            "成立时间": "待确认",
            "总部": "待确认",
            "员工数": "待确认",
            "所属行业": "待确认",
            "主营业务": "待确认",
            "上市状态": "待确认",
        }

    @classmethod
    def get_financial_data(cls, company_name: str, fiscal_year: Optional[str] = None) -> Dict[str, Any]:
        """
        获取公司财务数据

        Args:
            company_name: 公司名称
            fiscal_year: 财年（可选）

        Returns:
            财务数据字典
        """
        kb_company = cls.match_company(company_name)

        if kb_company and "财务数据" in cls.COMPANY_KNOWLEDGE[kb_company]:
            fin_data = cls.COMPANY_KNOWLEDGE[kb_company]["财务数据"]
            if fiscal_year and fiscal_year in fin_data:
                return {
                    "公司": kb_company,
                    "财年": fiscal_year,
                    "数据": fin_data[fiscal_year],
                }
            # 返回全部财务数据
            return {
                "公司": kb_company,
                "数据": fin_data,
            }

        # 返回通用财务数据
        return {
            "公司": company_name,
            "数据": {
                "说明": "暂无该公司的详细财务数据，请参考公开财报",
            },
        }


# ============================================================
# 工具 Schema 定义
# ============================================================

TOOL_SCHEMAS = {
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索网络信息，获取公司相关的最新资讯、财务数据、业务信息、竞争格局、技术动态等。适用于需要获取外部信息的场景。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，应具体明确，例如'商汤科技 2024年 营收'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    "company_lookup": {
        "type": "function",
        "function": {
            "name": "company_lookup",
            "description": "查询企业基本信息，包括成立时间、总部、员工数、行业、主营业务、创始人、上市信息等。用于快速获取公司画像。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": "公司名称，例如'商汤科技'",
                    },
                },
                "required": ["company_name"],
            },
        },
    },
    "financial_data": {
        "type": "function",
        "function": {
            "name": "financial_data",
            "description": "查询企业财务数据，包括营业收入、利润、毛利率、研发投入、营收结构等财务指标。用于财务维度的精准数据获取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "company_name": {
                        "type": "string",
                        "description": "公司名称，例如'商汤科技'",
                    },
                    "fiscal_year": {
                        "type": "string",
                        "description": "财年，例如'2024H1'或'2023'，可选参数",
                    },
                },
                "required": ["company_name"],
            },
        },
    },
}


# ============================================================
# 工具实现类
# ============================================================

class WebSearchTool:
    """
    通用网页搜索工具 - 智能模拟实现

    基于预定义知识库返回结构化搜索结果，支持多类别匹配和相关性排序。
    调用流程完全遵循 function calling 规范。
    """

    def __init__(self):
        """初始化搜索工具"""
        self.search_count = 0
        self.search_history = []

    def search(self, query: str, company_name: str) -> str:
        """
        执行搜索，返回结构化JSON结果

        Args:
            query: 搜索关键词
            company_name: 目标公司名称

        Returns:
            JSON格式的搜索结果字符串
        """
        self.search_count += 1
        self.search_history.append({"query": query, "company": company_name})

        # 模拟网络延迟
        time.sleep(0.3)

        try:
            # 匹配最相关的类别
            category = KnowledgeBase.match_category(query)

            # 获取搜索结果
            results = KnowledgeBase.get_search_results(company_name, category)

            # 根据query计算相关性并排序
            scored = []
            for r in results:
                score = 0
                text = r["title"] + r["snippet"]
                for word in query.split():
                    if word and word in text:
                        score += 1
                scored.append((score, r))
            scored.sort(key=lambda x: x[0], reverse=True)
            sorted_results = [r for _, r in scored]

            output = {
                "query": query,
                "company": company_name,
                "category": category,
                "total": len(sorted_results),
                "results": sorted_results,
                "search_time": now_ts(),
            }

            logger.debug(f"web_search: query={query}, company={company_name}, results={len(sorted_results)}")
            return json.dumps(output, ensure_ascii=False)

        except Exception as e:
            logger.error(f"web_search error: {e}")
            return json.dumps({
                "query": query,
                "company": company_name,
                "total": 0,
                "results": [],
                "error": str(e),
                "search_time": now_ts(),
            }, ensure_ascii=False)


class CompanyLookupTool:
    """
    企业基本信息查询工具 - 智能模拟实现

    提供公司的基本画像信息，包括成立时间、总部、员工数、行业、主营业务等。
    """

    def __init__(self):
        """初始化公司查询工具"""
        self.lookup_count = 0

    def lookup(self, company_name: str) -> str:
        """
        查询公司基本信息

        Args:
            company_name: 公司名称

        Returns:
            JSON格式的公司基本信息
        """
        self.lookup_count += 1
        time.sleep(0.2)  # 模拟延迟

        try:
            info = KnowledgeBase.get_company_info(company_name)

            output = {
                "company_name": company_name,
                "matched": KnowledgeBase.match_company(company_name) is not None,
                "basic_info": info,
                "query_time": now_ts(),
            }

            logger.debug(f"company_lookup: {company_name}")
            return json.dumps(output, ensure_ascii=False)

        except Exception as e:
            logger.error(f"company_lookup error: {e}")
            return json.dumps({
                "company_name": company_name,
                "matched": False,
                "basic_info": {},
                "error": str(e),
                "query_time": now_ts(),
            }, ensure_ascii=False)


class FinancialDataTool:
    """
    财务数据查询工具 - 智能模拟实现

    提供公司的财务指标数据，包括营收、利润、毛利率、研发投入等。
    """

    def __init__(self):
        """初始化财务数据工具"""
        self.query_count = 0

    def query(self, company_name: str, fiscal_year: Optional[str] = None) -> str:
        """
        查询公司财务数据

        Args:
            company_name: 公司名称
            fiscal_year: 财年（可选）

        Returns:
            JSON格式的财务数据
        """
        self.query_count += 1
        time.sleep(0.25)  # 模拟延迟

        try:
            fin_data = KnowledgeBase.get_financial_data(company_name, fiscal_year)

            output = {
                "company_name": company_name,
                "fiscal_year": fiscal_year or "all",
                "matched": KnowledgeBase.match_company(company_name) is not None,
                "financial_data": fin_data,
                "query_time": now_ts(),
            }

            logger.debug(f"financial_data: {company_name}, {fiscal_year}")
            return json.dumps(output, ensure_ascii=False)

        except Exception as e:
            logger.error(f"financial_data error: {e}")
            return json.dumps({
                "company_name": company_name,
                "fiscal_year": fiscal_year or "all",
                "matched": False,
                "financial_data": {},
                "error": str(e),
                "query_time": now_ts(),
            }, ensure_ascii=False)


# ============================================================
# 工具管理器
# ============================================================

class ToolManager:
    """
    工具管理器 - 统一管理所有工具的注册和调用

    负责：
    - 维护工具注册表
    - 执行工具调用
    - 参数校验
    - 错误处理
    - 调用统计
    """

    def __init__(self):
        """初始化工具管理器"""
        self.tools = {
            "web_search": WebSearchTool(),
            "company_lookup": CompanyLookupTool(),
            "financial_data": FinancialDataTool(),
        }
        self.call_stats = {}  # 工具调用统计

    def get_tool_schemas(self, tool_names: Optional[List[str]] = None) -> List[Dict]:
        """
        获取指定工具的schema列表

        Args:
            tool_names: 工具名称列表，None表示全部

        Returns:
            工具schema列表
        """
        if tool_names is None:
            tool_names = list(self.tools.keys())

        schemas = []
        for name in tool_names:
            if name in TOOL_SCHEMAS:
                schemas.append(TOOL_SCHEMAS[name])
        return schemas

    def execute_tool(self, tool_name: str, arguments_str: str, company_name: Optional[str] = None) -> str:
        """
        执行工具调用

        Args:
            tool_name: 工具名称
            arguments_str: JSON格式的参数字符串
            company_name: 公司名称（用于上下文）

        Returns:
            工具执行结果（JSON字符串）
        """
        # 统计调用次数
        self.call_stats[tool_name] = self.call_stats.get(tool_name, 0) + 1

        # 解析参数（支持字符串和dict两种类型）
        if isinstance(arguments_str, dict):
            args = arguments_str
        elif isinstance(arguments_str, str):
            try:
                args = json.loads(arguments_str)
            except json.JSONDecodeError as e:
                logger.warning(f"Tool {tool_name} arguments parse error: {e}")
                return json.dumps({"error": f"参数解析失败: {e}"}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"参数类型错误: {type(arguments_str)}"}, ensure_ascii=False)

        # 检查工具是否存在
        if tool_name not in self.tools:
            return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

        try:
            if tool_name == "web_search":
                query = args.get("query", "")
                if not query:
                    return json.dumps({"error": "缺少必填参数: query"}, ensure_ascii=False)
                ctx_company = args.get("company_name") or company_name or "未知公司"
                return self.tools["web_search"].search(query, ctx_company)

            elif tool_name == "company_lookup":
                cn = args.get("company_name") or company_name
                if not cn:
                    return json.dumps({"error": "缺少必填参数: company_name"}, ensure_ascii=False)
                return self.tools["company_lookup"].lookup(cn)

            elif tool_name == "financial_data":
                cn = args.get("company_name") or company_name
                if not cn:
                    return json.dumps({"error": "缺少必填参数: company_name"}, ensure_ascii=False)
                fiscal_year = args.get("fiscal_year")
                return self.tools["financial_data"].query(cn, fiscal_year)

            else:
                return json.dumps({"error": f"工具未实现: {tool_name}"}, ensure_ascii=False)

        except Exception as e:
            logger.error(f"Tool {tool_name} execution error: {e}")
            return json.dumps({"error": f"工具执行失败: {e}"}, ensure_ascii=False)

    def get_stats(self) -> Dict[str, int]:
        """获取工具调用统计"""
        return dict(self.call_stats)
