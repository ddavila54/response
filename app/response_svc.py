import asyncio
import json
import uuid
import yaml

from aiohttp import web
from aiohttp_jinja2 import template

from app.objects.secondclass.c_fact import Fact
from app.objects.c_operation import Operation
from app.objects.c_source import Source
from app.utility.base_service import BaseService


async def handle_link_completed(socket, path, services):
    data = json.loads(await socket.recv())
    paw = data['agent']['paw']
    data_svc = services.get('data_svc')

    agent = await data_svc.locate('agents', match=dict(paw=paw, access=data_svc.Access.RED))
    if agent:
        pid = data['pid']
        return await services.get('response_svc').respond_to_pid(pid, agent[0])


class ResponseService(BaseService):

    def __init__(self, services):
        self.log = self.add_service('response_svc', self)
        self.data_svc = services.get('data_svc')
        self.rest_svc = services.get('rest_svc')
        self.agents = []
        self.adversary = None
        self.abilities = []
        self.op = None

    @template('response.html')
    async def splash(self, request):
        abilities = [a for a in await self.data_svc.locate('abilities') if await a.which_plugin() == 'response']
        adversaries = [a for a in await self.data_svc.locate('adversaries') if await a.which_plugin() == 'response']
        await self._apply_adversary_config()
        return dict(abilities=abilities, adversaries=adversaries, auto_response=self.adversary)

    async def update_responder(self, request):
        data = dict(await request.json())
        self.set_config(name='response', prop='adversary', value=data['adversary_id'])
        await self._apply_adversary_config()
        await self._save_configurations()
        return web.json_response('complete')

    @staticmethod
    async def register_handler(event_svc):
        await event_svc.observe_event('link/completed', handle_link_completed)

    async def respond_to_pid(self, pid, red_agent):
        available_agents = await self.get_available_agents(red_agent)
        if not available_agents:
            return
        total_facts = []
        total_links = []

        for blue_agent in available_agents:
            agent_facts, agent_links = await self.run_abilities_on_agent(blue_agent, red_agent, pid)
            total_facts.extend(agent_facts)
            total_links.extend(agent_links)

        await self.save_to_operation(total_facts, total_links)

    async def get_available_agents(self, agent_to_match):
        await self.refresh_blue_agents_abilities()
        available_agents = [a for a in self.agents if a.host == agent_to_match.host]

        if not available_agents:
            self.log.debug('No available blue agents to respond to red action')
            return []

        return available_agents

    async def refresh_blue_agents_abilities(self):
        self.agents = await self.data_svc.locate('agents', match=dict(access=self.Access.BLUE))
        await self._apply_adversary_config()

        self.abilities = []
        for a in self.adversary.atomic_ordering:
            if a not in self.abilities:
                self.abilities.append(a)

    async def run_abilities_on_agent(self, blue_agent, red_agent, original_pid):
        facts = [Fact(trait='host.process.id', value=original_pid)]
        links = []
        relationships = []
        for ability_id in self.abilities:
            ability_facts, ability_links, ability_relationships = await self.run_ability_on_agent(blue_agent, red_agent, ability_id, facts, original_pid, relationships)
            links.extend(ability_links)
            facts.extend(ability_facts)
            relationships.extend(ability_relationships)
        return facts, links

    async def run_ability_on_agent(self, blue_agent, red_agent, ability_id, agent_facts, original_pid, relationships):
        links = await self.rest_svc.task_agent_with_ability(paw=blue_agent.paw, ability_id=ability_id,
                                                            obfuscator='plain-text', facts=agent_facts)
        await self.wait_for_link_completion(links, blue_agent)
        ability_facts = []
        ability_relationships = []
        for link in links:
            ability_relationships.extend(link.relationships)
            link.pin = int(original_pid)
            unique_facts = link.facts[1:]
            ability_facts.extend(self._filter_ability_facts(unique_facts, relationships + ability_relationships,
                                                            str(red_agent.pid), original_pid))
        return ability_facts, links, ability_relationships

    @staticmethod
    async def wait_for_link_completion(links, agent):
        for link in links:
            while not link.finish or link.can_ignore():
                await asyncio.sleep(3)
                if not agent.trusted:
                    break

    async def create_fact_source(self, facts):
        source_id = str(uuid.uuid4())
        source_name = 'blue-pid-{}'.format(source_id)
        return Source(id=source_id, name=source_name, facts=facts)

    async def save_to_operation(self, facts, links):
        if not self.op or await self.op.is_finished():
            source = await self.create_fact_source(facts)
            await self.create_operation(links=links, source=source)
        else:
            await self.update_operation(links)
        await self.get_service('data_svc').store(self.op)

    async def create_operation(self, links, source):
        planner = (await self.get_service('data_svc').locate('planners', match=dict(name='batch')))[0]
        await self.get_service('data_svc').store(source)
        blue_op_name = self.get_config(prop='op_name', name='response')
        self.op = Operation(name=blue_op_name, agents=self.agents, adversary=self.adversary,
                            source=source, access=self.Access.BLUE, planner=planner, state='running',
                            auto_close=False, jitter='1/4')
        self.op.set_start_details()
        await self.update_operation(links)

    async def update_operation(self, links):
        for link in links:
            link.operation = self.op.id
            self.op.add_link(link)

    async def _apply_adversary_config(self):
        blue_adversary = self.get_config(prop='adversary', name='response')
        self.adversary = (await self.data_svc.locate('adversaries', match=dict(adversary_id=blue_adversary)))[0]

    async def _save_configurations(self):
        with open('plugins/response/conf/response.yml', 'w') as config:
            config.write(yaml.dump(self.get_config(name='response')))

    def _filter_ability_facts(self, unique_facts, relationships, red_pid, original_pid):
        ability_facts = []
        for fact in unique_facts:
            if fact.trait == 'host.process.guid':
                if self._is_child_guid(relationships, red_pid, original_pid, fact):
                    ability_facts.append(fact)
            elif fact.trait == 'host.process.parentguid':
                if self._is_red_agent_guid(relationships, red_pid, fact):
                    ability_facts.append(fact)
            else:
                ability_facts.append(fact)
        return ability_facts

    @staticmethod
    def _is_child_guid(relationships, red_pid, original_pid, fact):
        for r in relationships:
            if r.edge == 'has_parentid' and \
                    (r.target.value.strip() == red_pid or r.target.value.strip() == original_pid) \
                    and r.source.value == fact.value:
                return True
        return False

    @staticmethod
    def _is_red_agent_guid(relationships, red_pid, fact):
        red_guid = [r.target.value for r in relationships if r.source.value.strip() == red_pid].pop()
        return fact.value == red_guid
