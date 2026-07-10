"""
多Agent深度研究系统 - 工具定义模块

包含三个核心工具：
- web_search: 通用网页搜索（基于DDGS真实搜索）
- company_lookup: 企业基本信息查询（基于搜索+Wikipedia）
- financial_data: 财务数据查询（基于yfinance真实数据）

所有工具均为真实实现，调用外部API/服务获取数据。
工具调用遵循 OpenAI function calling 规范，支持参数校验和错误处理。
"""

import json
import time
import logging
import re
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


def clean_html(text: str) -> str:
    """清理HTML标签"""
    if not text:
        return ""
    clean = re.compile(r'<[^>]+>')
    return clean.sub('', text)


# ============================================================
# 工具Schema定义
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
# 真实搜索工具实现
# ============================================================

class WebSearchTool:
    """
    通用网页搜索工具 - 真实实现

    基于DDGS (DuckDuckGo Search) 进行真实网页搜索，
    返回结构化搜索结果，支持多语言和相关性排序。
    """

    def __init__(self):
        """初始化搜索工具"""
        self.search_count = 0
        self.search_history = []
        self._ddgs = None

    def _get_ddgs(self):
        """延迟初始化DDGS客户端"""
        if self._ddgs is None:
            try:
                from ddgs import DDGS
                self._ddgs = DDGS()
            except ImportError:
                logger.warning("ddgs not available, falling back to mock search")
                self._ddgs = None
        return self._ddgs

    def search(self, query: str, company_name: str = "") -> str:
        """
        执行真实网页搜索，返回结构化JSON结果

        Args:
            query: 搜索关键词
            company_name: 目标公司名称（用于上下文）

        Returns:
            JSON格式的搜索结果字符串
        """
        self.search_count += 1
        self.search_history.append({"query": query, "company": company_name})

        start_time = time.time()

        try:
            ddgs = self._get_ddgs()

            if ddgs is not None:
                # 真实搜索
                results = []
                try:
                    search_results = list(ddgs.text(query, max_results=8))
                    for i, r in enumerate(search_results):
                        results.append({
                            "index": i + 1,
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "snippet": r.get("body", "")[:300],
                            "source": "web_search",
                        })
                except Exception as e:
                    logger.warning(f"DDGS search failed: {e}, trying English query")
                    # 尝试英文搜索
                    try:
                        en_query = query
                        if company_name and "商汤" in company_name:
                            en_query = query.replace("商汤科技", "SenseTime").replace("商汤", "SenseTime")
                        search_results = list(ddgs.text(en_query, max_results=8))
                        for i, r in enumerate(search_results):
                            results.append({
                                "index": i + 1,
                                "title": r.get("title", ""),
                                "url": r.get("href", ""),
                                "snippet": r.get("body", "")[:300],
                                "source": "web_search_en",
                            })
                    except Exception as e2:
                        logger.error(f"English search also failed: {e2}")
                        results = []

                elapsed_ms = int((time.time() - start_time) * 1000)

                output = {
                    "query": query,
                    "company": company_name,
                    "total": len(results),
                    "results": results,
                    "search_time": now_ts(),
                    "elapsed_ms": elapsed_ms,
                    "is_real_search": True,
                }

                logger.info(f"web_search: query='{query}', results={len(results)}, time={elapsed_ms}ms")
                return json.dumps(output, ensure_ascii=False)

            else:
                # 降级：使用备用搜索方式
                return self._fallback_search(query, company_name, start_time)

        except Exception as e:
            logger.error(f"web_search error: {e}")
            return json.dumps({
                "query": query,
                "company": company_name,
                "total": 0,
                "results": [],
                "error": str(e),
                "search_time": now_ts(),
                "is_real_search": False,
            }, ensure_ascii=False)

    def _fallback_search(self, query: str, company_name: str, start_time: float) -> str:
        """备用搜索方式 - 通过Wikipedia API等"""
        results = []
        try:
            import requests
            # 尝试Wikipedia搜索
            wiki_url = "https://en.wikipedia.org/w/api.php"
            params = {
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 5,
            }
            resp = requests.get(wiki_url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                for i, item in enumerate(data.get("query", {}).get("search", [])):
                    results.append({
                        "index": i + 1,
                        "title": item.get("title", ""),
                        "url": f"https://en.wikipedia.org/wiki/{item.get('title', '').replace(' ', '_')}",
                        "snippet": clean_html(item.get("snippet", ""))[:300],
                        "source": "wikipedia",
                    })
        except Exception as e:
            logger.warning(f"Wikipedia fallback search failed: {e}")

        elapsed_ms = int((time.time() - start_time) * 1000)
        output = {
            "query": query,
            "company": company_name,
            "total": len(results),
            "results": results,
            "search_time": now_ts(),
            "elapsed_ms": elapsed_ms,
            "is_real_search": len(results) > 0,
            "note": "Used Wikipedia fallback search",
        }
        return json.dumps(output, ensure_ascii=False)


class CompanyLookupTool:
    """
    企业基本信息查询工具 - 真实实现

    基于网页搜索和Wikipedia获取公司基本信息，
    包括成立时间、总部、员工数、行业、主营业务等。
    """

    def __init__(self):
        """初始化公司查询工具"""
        self.lookup_count = 0
        self._cache = {}

    def lookup(self, company_name: str) -> str:
        """
        查询公司基本信息

        Args:
            company_name: 公司名称

        Returns:
            JSON格式的公司信息字符串
        """
        self.lookup_count += 1

        if company_name in self._cache:
            return self._cache[company_name]

        start_time = time.time()

        try:
            info = self._fetch_company_info(company_name)
            elapsed_ms = int((time.time() - start_time) * 1000)

            output = {
                "company_name": company_name,
                "info": info,
                "query_time": now_ts(),
                "elapsed_ms": elapsed_ms,
                "is_real_data": True,
            }

            self._cache[company_name] = json.dumps(output, ensure_ascii=False)
            return json.dumps(output, ensure_ascii=False)

        except Exception as e:
            logger.error(f"company_lookup error: {e}")
            return json.dumps({
                "company_name": company_name,
                "info": {},
                "error": str(e),
                "query_time": now_ts(),
                "is_real_data": False,
            }, ensure_ascii=False)

    def _fetch_company_info(self, company_name: str) -> Dict:
        """从多个来源获取公司信息"""
        info = {}

        # 尝试yfinance获取上市公司信息
        ticker_symbol = self._find_ticker(company_name)
        if ticker_symbol:
            try:
                import yfinance as yf
                ticker = yf.Ticker(ticker_symbol)
                yf_info = ticker.info

                info["ticker"] = ticker_symbol
                info["公司全称"] = yf_info.get("longName", yf_info.get("shortName", ""))
                info["行业"] = yf_info.get("sector", "")
                info["子行业"] = yf_info.get("industry", "")
                info["总部所在城市"] = yf_info.get("city", "")
                info["国家"] = yf_info.get("country", "")
                info["员工数"] = yf_info.get("numberOfEmployees", "")
                info["市值"] = yf_info.get("marketCap", "")
                info["网站"] = yf_info.get("website", "")
                info["主营业务"] = yf_info.get("longBusinessSummary", "")[:500]
                info["上市交易所"] = yf_info.get("exchange", "")
            except Exception as e:
                logger.warning(f"yfinance lookup failed: {e}")

        # 补充搜索结果
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                search_query = f"{company_name} 公司简介 成立时间 创始人"
                results = list(ddgs.text(search_query, max_results=3))
                if results:
                    info["搜索摘要"] = results[0].get("body", "")[:400]
                    info["信息来源"] = results[0].get("href", "")
        except Exception as e:
            logger.warning(f"Company search failed: {e}")

        return info

    def _find_ticker(self, company_name: str) -> Optional[str]:
        """根据公司名查找股票代码"""
        # 常见公司映射
        company_tickers = {
            "商汤科技": "0020.HK",
            "商汤": "0020.HK",
            "SenseTime": "0020.HK",
            "腾讯": "0700.HK",
            "腾讯控股": "0700.HK",
            "阿里巴巴": "9988.HK",
            "百度": "9888.HK",
            "京东": "9618.HK",
            "美团": "3690.HK",
            "字节跳动": None,  # 未上市
            "科大讯飞": "002230.SZ",
            "海康威视": "002415.SZ",
            "旷视科技": None,  # 需确认
        }

        for name, ticker in company_tickers.items():
            if name in company_name or company_name in name:
                return ticker

        # 尝试搜索
        try:
            from ddgs import DDGS
            with DDGS() as ddgs:
                results = list(ddgs.text(f"{company_name} stock ticker Hong Kong", max_results=3))
                for r in results:
                    # 从搜索结果中提取股票代码
                    text = r.get("title", "") + r.get("body", "")
                    import re
                    hk_match = re.search(r'(\d{4})\.HK', text, re.IGNORECASE)
                    if hk_match:
                        return hk_match.group(0)
        except Exception:
            pass

        return None


class FinancialDataTool:
    """
    财务数据查询工具 - 真实实现

    基于yfinance获取真实财务数据，
    包括营业收入、利润、毛利率、资产负债等。
    """

    def __init__(self):
        """初始化财务数据工具"""
        self.query_count = 0
        self._cache = {}

    def query(self, company_name: str, fiscal_year: str = "") -> str:
        """
        查询公司财务数据

        Args:
            company_name: 公司名称
            fiscal_year: 财年（可选）

        Returns:
            JSON格式的财务数据字符串
        """
        self.query_count += 1

        cache_key = f"{company_name}_{fiscal_year}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        start_time = time.time()

        try:
            data = self._fetch_financial_data(company_name, fiscal_year)
            elapsed_ms = int((time.time() - start_time) * 1000)

            output = {
                "company_name": company_name,
                "fiscal_year": fiscal_year,
                "data": data,
                "query_time": now_ts(),
                "elapsed_ms": elapsed_ms,
                "is_real_data": True,
            }

            self._cache[cache_key] = json.dumps(output, ensure_ascii=False)
            return json.dumps(output, ensure_ascii=False)

        except Exception as e:
            logger.error(f"financial_data error: {e}")
            return json.dumps({
                "company_name": company_name,
                "fiscal_year": fiscal_year,
                "data": {},
                "error": str(e),
                "query_time": now_ts(),
                "is_real_data": False,
            }, ensure_ascii=False)

    def _fetch_financial_data(self, company_name: str, fiscal_year: str) -> Dict:
        """获取真实财务数据"""
        result = {}

        # 查找股票代码
        ticker_symbol = self._find_ticker(company_name)
        if not ticker_symbol:
            result["note"] = "未找到对应股票代码，无法获取财务数据"
            return result

        result["ticker"] = ticker_symbol

        try:
            import yfinance as yf
            ticker = yf.Ticker(ticker_symbol)

            # 基本市场数据
            info = ticker.info
            result["market_data"] = {
                "当前股价": info.get("currentPrice", ""),
                "市值": info.get("marketCap", ""),
                "52周最高": info.get("fiftyTwoWeekHigh", ""),
                "52周最低": info.get("fiftyTwoWeekLow", ""),
                "市盈率": info.get("trailingPE", ""),
                "市净率": info.get("priceToBook", ""),
            }

            # 利润表
            try:
                income_stmt = ticker.financials
                if income_stmt is not None and not income_stmt.empty:
                    result["income_statement"] = {}
                    # 获取最近几年的数据
                    for col in income_stmt.columns[:4]:  # 最近4年
                        year_str = str(col.year) if hasattr(col, 'year') else str(col)
                        year_data = {}
                        for idx in income_stmt.index:
                            val = income_stmt.loc[idx, col]
                            if val and str(val) != 'nan':
                                year_data[idx] = val
                        result["income_statement"][year_str] = year_data
            except Exception as e:
                logger.warning(f"Income statement fetch failed: {e}")

            # 资产负债表
            try:
                balance_sheet = ticker.balance_sheet
                if balance_sheet is not None and not balance_sheet.empty:
                    result["balance_sheet"] = {}
                    for col in balance_sheet.columns[:4]:
                        year_str = str(col.year) if hasattr(col, 'year') else str(col)
                        year_data = {}
                        for idx in balance_sheet.index:
                            val = balance_sheet.loc[idx, col]
                            if val and str(val) != 'nan':
                                year_data[idx] = val
                        result["balance_sheet"][year_str] = year_data
            except Exception as e:
                logger.warning(f"Balance sheet fetch failed: {e}")

            # 关键财务指标
            result["key_metrics"] = {
                "毛利率": info.get("grossMargins", ""),
                "营业利润率": info.get("operatingMargins", ""),
                "净利润率": info.get("profitMargins", ""),
                "营收增长率": info.get("revenueGrowth", ""),
                "盈利增长率": info.get("earningsGrowth", ""),
                "ROE": info.get("returnOnEquity", ""),
                "ROA": info.get("returnOnAssets", ""),
                "研发投入占比": info.get("researchDevelopment", ""),  # 绝对值
            }

            # 现金流量
            try:
                cashflow = ticker.cashflow
                if cashflow is not None and not cashflow.empty:
                    result["cash_flow"] = {}
                    for col in cashflow.columns[:3]:
                        year_str = str(col.year) if hasattr(col, 'year') else str(col)
                        year_data = {}
                        for idx in cashflow.index:
                            val = cashflow.loc[idx, col]
                            if val and str(val) != 'nan':
                                year_data[idx] = val
                        result["cash_flow"][year_str] = year_data
            except Exception as e:
                logger.warning(f"Cash flow fetch failed: {e}")

        except Exception as e:
            logger.error(f"yfinance financial data error: {e}")
            result["error"] = str(e)

        return result

    def _find_ticker(self, company_name: str) -> Optional[str]:
        """根据公司名查找股票代码"""
        company_tickers = {
            "商汤科技": "0020.HK",
            "商汤": "0020.HK",
            "SenseTime": "0020.HK",
            "腾讯": "0700.HK",
            "腾讯控股": "0700.HK",
            "阿里巴巴": "9988.HK",
            "百度": "9888.HK",
            "京东": "9618.HK",
            "美团": "3690.HK",
            "科大讯飞": "002230.SZ",
            "海康威视": "002415.SZ",
        }

        for name, ticker in company_tickers.items():
            if name in company_name or company_name in name:
                return ticker

        return None


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
            arguments_str: 参数JSON字符串
            company_name: 公司名称（上下文）

        Returns:
            工具返回结果字符串
        """
        # 统计调用
        self.call_stats[tool_name] = self.call_stats.get(tool_name, 0) + 1

        try:
            arguments = json.loads(arguments_str)
        except json.JSONDecodeError:
            return json.dumps({"error": "Invalid arguments format", "tool": tool_name}, ensure_ascii=False)

        if tool_name == "web_search":
            query = arguments.get("query", "")
            if not query:
                return json.dumps({"error": "query parameter is required"}, ensure_ascii=False)
            return self.tools["web_search"].search(query, company_name or "")

        elif tool_name == "company_lookup":
            comp_name = arguments.get("company_name", company_name or "")
            if not comp_name:
                return json.dumps({"error": "company_name parameter is required"}, ensure_ascii=False)
            return self.tools["company_lookup"].lookup(comp_name)

        elif tool_name == "financial_data":
            comp_name = arguments.get("company_name", company_name or "")
            fiscal_year = arguments.get("fiscal_year", "")
            if not comp_name:
                return json.dumps({"error": "company_name parameter is required"}, ensure_ascii=False)
            return self.tools["financial_data"].query(comp_name, fiscal_year)

        else:
            return json.dumps({"error": f"Unknown tool: {tool_name}"}, ensure_ascii=False)

    def get_stats(self) -> Dict:
        """获取工具调用统计"""
        return self.call_stats.copy()


# ============================================================
# 快速测试
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("工具模块测试")
    print("=" * 60)

    tool_mgr = ToolManager()

    # 测试web_search
    print("\n1. 测试 web_search:")
    result = tool_mgr.execute_tool("web_search", json.dumps({"query": "SenseTime 2024 annual revenue"}), "商汤科技")
    data = json.loads(result)
    print(f"   结果数量: {data.get('total', 0)}")
    print(f"   是否真实搜索: {data.get('is_real_search', False)}")
    for r in data.get("results", [])[:2]:
        print(f"   - {r.get('title', '')[:50]}")

    # 测试company_lookup
    print("\n2. 测试 company_lookup:")
    result = tool_mgr.execute_tool("company_lookup", json.dumps({"company_name": "商汤科技"}))
    data = json.loads(result)
    info = data.get("info", {})
    print(f"   公司全称: {info.get('公司全称', 'N/A')}")
    print(f"   行业: {info.get('行业', 'N/A')}")
    print(f"   员工数: {info.get('员工数', 'N/A')}")
    print(f"   是否真实数据: {data.get('is_real_data', False)}")

    # 测试financial_data
    print("\n3. 测试 financial_data:")
    result = tool_mgr.execute_tool("financial_data", json.dumps({"company_name": "商汤科技"}))
    data = json.loads(result)
    fin_data = data.get("data", {})
    print(f"   股票代码: {fin_data.get('ticker', 'N/A')}")
    market = fin_data.get("market_data", {})
    print(f"   市值: {market.get('市值', 'N/A')}")
    print(f"   是否真实数据: {data.get('is_real_data', False)}")

    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
