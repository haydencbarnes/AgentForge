import uuid
from typing import Dict, Any
from ..llm import LLM
from ..logs.logger_config import Logger
from .. import config

from ..utils.storage_interface import StorageInterface

from termcolor import cprint
from colorama import init
init(autoreset=True)


def _calculate_next_task_order(this_task_order):
    return int(this_task_order) + 1


def _get_data(key, loader, kwargs, data, invert_logic=False):
    if ((key not in kwargs) and not invert_logic) or ((key in kwargs) and invert_logic):
        db_data = loader()
        if db_data is not None:
            data.update(db_data)
    return data


def _handle_prompt_type(prompts, prompt_type):
    """Handle each type of prompt and return template and vars."""
    prompt_data = prompts.get(prompt_type, {})
    if prompt_data:
        return [(prompt_data['template'], prompt_data['vars'])]
    return []


def _load_agent_data(agent_name):
    # Load persona data
    persona_data = config.persona()

    agent = persona_data[agent_name]
    defaults = persona_data['Defaults']

    api = agent.get('API', defaults['API'])
    params = agent.get('Params', defaults['Params'])

    # Initialize agent data
    agent_data: Dict[str, Any] = dict(
        name=agent_name,
        llm=config.get_llm(api, agent_name),
        objective=persona_data['Objective'],
        prompts=agent['Prompts'],
        params=params,
        storage=StorageInterface().storage_utils,
    )

    # ETHOS
    if "HeuristicImperatives" in persona_data:
        imperatives = persona_data["HeuristicImperatives"]
        agent_data.update(heuristic_imperatives=imperatives)

    return agent_data


def remove_prompt_if_none(prompts, kwargs, prompt_type, kwarg_key):
    if prompts.get(prompt_type) and kwargs.get(kwarg_key) is None:
        prompts.pop(prompt_type)


def _render_template(template, variables, data):
    temp = template.format(
        **{k: v for k, v in data.items() if k in variables}
    )

    return temp


def _show_task(data):
    if 'task' in data:
        cprint(f'\nTask: {data["task"]}', 'green', attrs=['dark'])


def _set_task_order(data):
    task_order = data.get('this_task_order')
    if task_order is not None:
        data['next_task_order'] = _calculate_next_task_order(task_order)


class Agent:
    def __init__(self, agent_name=None, log_level="info"):
        if agent_name is None:
            agent_name = self.__class__.__name__

        self.agent_data = _load_agent_data(agent_name)
        self.storage = self.agent_data['storage']
        self.logger = Logger(name=agent_name)
        self.logger.set_level(log_level)

    def run(self, bot_id=None, **kwargs):
        agent_name = self.__class__.__name__

        cprint(f"\n{agent_name} - Running Agent...", 'red', attrs=['bold'])

        data = self.load_and_process_data(**kwargs)
        prompts = self.generate_prompt(**data)
        result = self.run_llm(prompts)
        parsed_data = self.parse_output(result, bot_id, data)
        output = self.save_parsed_data(parsed_data)

        cprint(f"\n{agent_name} - Agent Done...\n", 'red', attrs=['bold'])

        return output

    def generate_prompt(self, **kwargs):
        # Load Prompts from Persona Data
        prompts = self.agent_data['prompts'].copy()
        templates = []

        # Handle system prompt
        system_prompt = prompts['SystemPrompt']
        templates.append((system_prompt["template"], system_prompt["vars"]))

        # Handle other types of prompts
        remove_prompt_if_none(prompts, kwargs, 'ContextPrompt', 'context')
        remove_prompt_if_none(prompts, kwargs, 'FeedbackPrompt', 'feedback')

        for prompt_type in ['ContextPrompt', 'FeedbackPrompt', 'InstructionPrompt']:
            templates.extend(_handle_prompt_type(prompts, prompt_type))

        # Render Prompts
        prompts = [
            _render_template(template, variables, data=kwargs)
            for template, variables in templates
        ]

        self.logger.log(f"Prompt:\n{prompts}", 'debug')

        return prompts

    def get_task_list(self):
        return self.storage.load_collection({'collection_name': "Tasks",
                                             'include': ["documents", "metadatas"]})

    def load_and_process_data(self, **kwargs):
        context_data = kwargs.get('context') or {}
        context = context_data.get('result', None)
        task_result_data = kwargs.get('task_result') or {}
        task_result = task_result_data.get('result', None)

        # Load data
        data = {}
        data.update(self.agent_data, **kwargs)

        data = _get_data("task", self.load_current_task, kwargs, data)
        data = _get_data("task_result", lambda: {'result': task_result}, kwargs, data, invert_logic=True)
        data.update({'context': context})

        _set_task_order(data)
        _show_task(data)

        return data

    def load_current_task(self):
        task_list = self.get_task_list()
        task = task_list['documents'][0]
        return {'task': task}

    def load_result_data(self):
        result_collection = self.storage.load_collection({
            'collection_name': "Results",
            'include': ["documents"],
        })
        result = result_collection[0] if result_collection else ["No results found"]
        self.logger.log(f"Result Data Loaded:\n{result}", 'debug')

        return result

    def parse_output(self, result, bot_id, data):  # Remember to incorporate bot_if and data later on
        return {"result": result}

    def run_llm(self, prompt):
        model: LLM = self.agent_data['llm']
        params = self.agent_data.get("params", {})
        return model.generate_text(prompt, **params,).strip()

    def save_parsed_data(self, parsed_data):
        save_operations = {
            "result": (self.save_results, lambda data: data),
            "Tasks": (lambda tasks: self.save_tasks(tasks, [task['Description'] for task in tasks]),
                      lambda data: data["Tasks"]),
            "status": (lambda status: self.save_status(status, parsed_data["task"]["task_id"],
                                                       parsed_data["task"]["description"],
                                                       parsed_data["task"]["order"]), lambda data: data)
        }

        output = None
        for key, (save_function, data_selector) in save_operations.items():
            if key in parsed_data:
                save_function(parsed_data[key])
                output = data_selector(parsed_data)

        return output

    def save_results(self, result, collection_name="Results"):
        self.storage.save_memory({
            'data': [result],
            'collection_name': collection_name,
        })

    def save_status(self, status, task_id, text, task_order):
        self.logger.log(
            f"\nSave Task: {text}"
            f"\nSave ID: {task_id}"
            f"\nSave Order: {task_order}"
            f"\nSave Status: {status}",
            'debug'
        )
        params = {
            'collection_name': "Tasks",
            'ids': [task_id],
            'data': [text],
            'metadata': [{
                "Status": status,
                "Description": text,
                "Order": task_order,
            }]
        }
        self.storage.update_memory(params)

    def save_tasks(self, ordered_results, task_desc_list):
        collection_name = "Tasks"
        self.storage.clear_collection(collection_name)

        metadatas = [{
            "Status": "not completed",
            "Description": task["Description"],
            "List_ID": str(uuid.uuid4()),
            "Order": task["Order"]
        } for task in ordered_results]

        task_orders = [task["Order"] for task in ordered_results]

        params = {
            "collection_name": collection_name,
            "ids": [str(order) for order in task_orders],
            "data": task_desc_list,
            "metadata": metadatas,
        }

        self.storage.save_memory(params)

