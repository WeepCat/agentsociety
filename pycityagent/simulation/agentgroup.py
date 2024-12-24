import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import uuid
import fastavro
import ray
from uuid import UUID
from pycityagent.agent import Agent, CitizenAgent, InstitutionAgent
from pycityagent.economy.econ_client import EconomyClient
from pycityagent.environment.simulator import Simulator
from pycityagent.llm.llm import LLM
from pycityagent.llm.llmconfig import LLMConfig
from pycityagent.message import Messager
from pycityagent.utils import STATUS_SCHEMA, PROFILE_SCHEMA, DIALOG_SCHEMA, SURVEY_SCHEMA, INSTITUTION_STATUS_SCHEMA
from typing import Any

logger = logging.getLogger("pycityagent")

@ray.remote
class AgentGroup:
    def __init__(self, agents: list[Agent], config: dict, exp_id: str|UUID, enable_avro: bool, avro_path: Path, logging_level: int = logging.WARNING):
        logger.setLevel(logging_level)
        self._uuid = str(uuid.uuid4())
        self.agents = agents
        self.config = config
        self.exp_id = exp_id
        self.enable_avro = enable_avro
        self.avro_path = avro_path / f"{self._uuid}"
        if enable_avro:
            self.avro_path.mkdir(parents=True, exist_ok=True)
            self.avro_file = {
                "profile": self.avro_path / f"profile.avro",
                "dialog": self.avro_path / f"dialog.avro",
                "status": self.avro_path / f"status.avro",
                "survey": self.avro_path / f"survey.avro",
            }
            
        self.messager = Messager(
            hostname=config["simulator_request"]["mqtt"]["server"],
            port=config["simulator_request"]["mqtt"]["port"],
            username=config["simulator_request"]["mqtt"].get("username", None),
            password=config["simulator_request"]["mqtt"].get("password", None),
        )
        self.initialized = False
        self.id2agent = {}
        # Step:1 prepare LLM client
        llmConfig = LLMConfig(config["llm_request"])
        logger.info(f"-----Creating LLM client in AgentGroup {self._uuid} ...")
        self.llm = LLM(llmConfig)

        # Step:2 prepare Simulator
        logger.info(f"-----Creating Simulator in AgentGroup {self._uuid} ...")
        self.simulator = Simulator(config["simulator_request"])

        # Step:3 prepare Economy client
        if "economy" in config["simulator_request"]:
            logger.info(f"-----Creating Economy client in AgentGroup {self._uuid} ...")
            self.economy_client = EconomyClient(
                config["simulator_request"]["economy"]["server"]
            )
        else:
            self.economy_client = None

        for agent in self.agents:
            agent.set_exp_id(self.exp_id)
            agent.set_llm_client(self.llm)
            agent.set_simulator(self.simulator)
            if self.economy_client is not None:
                agent.set_economy_client(self.economy_client)
            agent.set_messager(self.messager)
            if self.enable_avro:
                agent.set_avro_file(self.avro_file)

    async def init_agents(self):
        logger.debug(f"-----Initializing Agents in AgentGroup {self._uuid} ...")
        logger.debug(f"-----Binding Agents to Simulator in AgentGroup {self._uuid} ...")
        for agent in self.agents:
            await agent.bind_to_simulator()
        self.id2agent = {agent._uuid: agent for agent in self.agents}
        logger.debug(f"-----Binding Agents to Messager in AgentGroup {self._uuid} ...")
        await self.messager.connect()
        if self.messager.is_connected():
            await self.messager.start_listening()
            for agent in self.agents:
                agent.set_messager(self.messager)
                topic = f"exps/{self.exp_id}/agents/{agent._uuid}/agent-chat"
                await self.messager.subscribe(topic, agent)
                topic = f"exps/{self.exp_id}/agents/{agent._uuid}/user-chat"
                await self.messager.subscribe(topic, agent)
                topic = f"exps/{self.exp_id}/agents/{agent._uuid}/user-survey"
                await self.messager.subscribe(topic, agent)
                topic = f"exps/{self.exp_id}/agents/{agent._uuid}/gather"
                await self.messager.subscribe(topic, agent)
        self.message_dispatch_task = asyncio.create_task(self.message_dispatch())
        if self.enable_avro:
            logger.debug(f"-----Creating Avro files in AgentGroup {self._uuid} ...")
            # profile
            if not issubclass(type(self.agents[0]), InstitutionAgent):
                filename = self.avro_file["profile"]
                with open(filename, "wb") as f:
                    profiles = []
                    for agent in self.agents:
                        profile = await agent.memory._profile.export()
                        profile = profile[0]
                        profile['id'] = agent._uuid
                        profiles.append(profile)
                    fastavro.writer(f, PROFILE_SCHEMA, profiles)

            # dialog
            filename = self.avro_file["dialog"]
            with open(filename, "wb") as f:
                dialogs = []
                fastavro.writer(f, DIALOG_SCHEMA, dialogs)

            # status
            filename = self.avro_file["status"]
            with open(filename, "wb") as f:
                statuses = []
                if not issubclass(type(self.agents[0]), InstitutionAgent):
                    fastavro.writer(f, STATUS_SCHEMA, statuses)
                else:
                    fastavro.writer(f, INSTITUTION_STATUS_SCHEMA, statuses)

            # survey
            filename = self.avro_file["survey"]
            with open(filename, "wb") as f:
                surveys = []
                fastavro.writer(f, SURVEY_SCHEMA, surveys)
        self.initialized = True
        logger.debug(f"-----AgentGroup {self._uuid} initialized")

    async def gather(self, content: str):
        logger.debug(f"-----Gathering {content} from all agents in group {self._uuid}")
        results = {}
        for agent in self.agents:
            results[agent._uuid] = await agent.memory.get(content)
        return results

    async def update(self, target_agent_uuid: str, target_key: str, content: Any):
        logger.debug(f"-----Updating {target_key} for agent {target_agent_uuid} in group {self._uuid}")
        agent = self.id2agent[target_agent_uuid]
        await agent.memory.update(target_key, content)

    async def message_dispatch(self):
        logger.debug(f"-----Starting message dispatch for group {self._uuid}")
        while True:
            if not self.messager.is_connected():
                logger.warning("Messager is not connected. Skipping message processing.")

            # Step 1: 获取消息
            messages = await self.messager.fetch_messages()
            logger.info(f"Group {self._uuid} received {len(messages)} messages")

            # Step 2: 分发消息到对应的 Agent
            for message in messages:
                topic = message.topic.value
                payload = message.payload

                # 添加解码步骤，将bytes转换为str
                if isinstance(payload, bytes):
                    payload = payload.decode("utf-8")
                    payload = json.loads(payload)

                # 提取 agent_id（主题格式为 "exps/{exp_id}/agents/{agent_uuid}/{topic_type}"）
                _, _, _, agent_uuid, topic_type = topic.strip("/").split("/")
                    
                if agent_uuid in self.id2agent:
                    agent = self.id2agent[agent_uuid]
                    # topic_type: agent-chat, user-chat, user-survey, gather
                    if topic_type == "agent-chat":
                        await agent.handle_agent_chat_message(payload)
                    elif topic_type == "user-chat":
                        await agent.handle_user_chat_message(payload)
                    elif topic_type == "user-survey":
                        await agent.handle_user_survey_message(payload)
                    elif topic_type == "gather":
                        await agent.handle_gather_message(payload)

            await asyncio.sleep(0.5)

    async def save_status(self):
        if self.enable_avro:
            logger.debug(f"-----Saving status for group {self._uuid}")
            avros = []
            if not issubclass(type(self.agents[0]), InstitutionAgent):
                for agent in self.agents:
                    position = await agent.memory.get("position")
                    lng = position["longlat_position"]["longitude"]
                    lat = position["longlat_position"]["latitude"]
                    if "aoi_position" in position:
                        parent_id = position["aoi_position"]["aoi_id"]
                    elif "lane_position" in position:
                        parent_id = position["lane_position"]["lane_id"]
                    else:
                        # BUG: 需要处理
                        parent_id = -1
                    needs = await agent.memory.get("needs")
                    action = await agent.memory.get("current_step")
                    action = action["intention"]
                    avro = {
                        "id": agent._uuid,
                        "day": await self.simulator.get_simulator_day(),
                        "t": await self.simulator.get_simulator_second_from_start_of_day(),
                        "lng": lng,
                        "lat": lat,
                        "parent_id": parent_id,
                        "action": action,
                        "hungry": needs["hungry"],
                        "tired": needs["tired"],
                        "safe": needs["safe"],
                        "social": needs["social"],
                        "created_at": int(datetime.now().timestamp() * 1000),
                    }
                    avros.append(avro)
                with open(self.avro_file["status"], "a+b") as f:
                    fastavro.writer(f, STATUS_SCHEMA, avros, codec="snappy")
            else:
                for agent in self.agents:
                    avro = {
                        "id": agent._uuid,
                        "day": await self.simulator.get_simulator_day(),
                        "t": await self.simulator.get_simulator_second_from_start_of_day(),
                        "type": await agent.memory.get("type"),
                        "nominal_gdp": await agent.memory.get("nominal_gdp"),
                        "real_gdp": await agent.memory.get("real_gdp"),
                        "unemployment": await agent.memory.get("unemployment"),
                        "wages": await agent.memory.get("wages"),
                        "prices": await agent.memory.get("prices"),
                        "inventory": await agent.memory.get("inventory"),
                        "price": await agent.memory.get("price"),
                        "interest_rate": await agent.memory.get("interest_rate"),
                        "bracket_cutoffs": await agent.memory.get("bracket_cutoffs"),
                        "bracket_rates": await agent.memory.get("bracket_rates"),
                        "employees": await agent.memory.get("employees"),
                        "customers": await agent.memory.get("customers"),
                    }
                    avros.append(avro)
                with open(self.avro_file["status"], "a+b") as f:
                    fastavro.writer(f, INSTITUTION_STATUS_SCHEMA, avros, codec="snappy")

    async def step(self):
        if not self.initialized:
            await self.init_agents()

        tasks = [agent.run() for agent in self.agents]
        await asyncio.gather(*tasks)
        await self.save_status()

    async def run(self, day: int = 1):
        """运行模拟器

        Args:
            day: 运行天数,默认为1天
        """
        try:
            # 获取开始时间
            start_time = await self.simulator.get_time()
            start_time = int(start_time)
            # 计算结束时间（秒）
            end_time = start_time + day * 24 * 3600  # 将天数转换为秒

            while True:
                current_time = await self.simulator.get_time()
                current_time = int(current_time)
                if current_time >= end_time:
                    break
                await self.step()

        except Exception as e:
            logger.error(f"模拟器运行错误: {str(e)}")
            raise
