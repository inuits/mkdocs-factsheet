# -*- flycheck-python-pylint-executable: "pylint" -*-
# pylint: disable=invalid-name, missing-docstring, too-few-public-methods

from __future__ import division, print_function
from collections import defaultdict, OrderedDict
from fnmatch import fnmatch
from functools import partial
from os.path import getmtime
import traceback

from anytree import NodeMixin, PreOrderIter
from anytree.search import find_by_attr, findall_by_attr
from mkdocs.plugins import BasePlugin
from mkdocs.config import config_options
import yaml

try:
  basestring
except NameError:
  basestring = str

class FactsheetPlugin(BasePlugin):
    config_scheme = (('sheets', config_options.Type(dict)),)

    def __init__(self, *args, **kwargs):
        super(FactsheetPlugin, self).__init__(*args, **kwargs)
        self.facts_list = OrderedDict()
        self.current = None
        self.page = None

    def on_pre_build(self, _):
        new = OrderedDict()
        for glob, path in self.config.get('sheets').items():
            new[glob] = [path, None, None]
        for glob, tup in self.facts_list:
            if glob not in new:
                continue
            if tup[2] is not None and tup[0] == new[glob][0] and \
               getmtime(tup[0]) <= tup[2]:
                new[glob][1] = tup[1]
                new[glob][2] = tup[2]
        self.facts_list = new

    def on_page_markdown(self, md, page, **_kwargs):
        self.page = page
        self.current = None
        output = []
        for line in md.splitlines():
            if not line.startswith("!factsheet"):
                output.append(line)
                continue
            try:
                output.append(self.dispatch(line[10:]))
            except Exception: #pylint: disable=broad-except
                traceback.print_exc()
                print('Ignoring directive "%s"' % line)
                output.append(line)
        return '\n'.join(output)

    def facts(self):
        if self.current is not None:
            return self.current

        for glob, sheet in self.facts_list.items():
            if fnmatch(self.page.abs_url, glob):
                if sheet[1] is None:
                    with open(sheet[0]) as f:
                        sheet[1] = Facts(yaml.load(f))
                return sheet[1]

        raise ValueError('File %s does not match any factsheet glob' % self.page.abs_url)

    def dispatch(self, line):
        parts = line.strip().split(':', 1)
        command, rest = parts[0], ''.join(parts[1:])
        dispatch_dict = {
            'tenant': self.render_tenant,
            'component': self.render_component,
            'monitoring': self.render_monitoring,
            'all': self.render_overview,
            'overview': self.render_overview}
        if command in dispatch_dict:
            return dispatch_dict[command](rest)
        else:
            raise ValueError('Unknown factsheet command %s' % command)

    def render_tenant(self, name):
        #Infobox, Menu (component -> entire infobox)
        t = self.facts().tenant(name)
        out = t.all_props(props_tenant).render_html()
        out += '\n## Components\n'
        for (c, origin) in t.all_components():
            out += '\n### %s' % c.human_name()
            if origin is not t:
                out += ' <small>(from %s)</small>' % origin.name
            out += '\n'
            out += c.all_props().render_html()
        return out

    def render_component(self, name):
        #Infobox + menu (tenant -> deploy info)
        if len(name.split(',')) > 1:
            out = ''
            for n in name.split(','):
                c = self.facts().component(n)
                out += '\n## ' + c[0].human_name() + '\n'
                out += self.render_component(n)
            return out

        c = self.facts().component(name)
        if not c:
            raise ValueError('Unknown component %s' % name)
        props = c[0].all_props(props_base)
        props['tenants'] = []
        for t in self.facts().component_refs(name):
            props['tenants'] += [Property(self.facts(), '', 'tenants', t.docs_link())]
        return props.render_html()

    def render_monitoring(self, _):
        #Menu (tenant -> monitoring)
        out = ''
        for t in PreOrderIter(self.facts().tenant_tree):
            if t.name == '_root':
                continue
            for k, v in t.all_props(props_tenant).items():
                if k != 'monitoring':
                    continue
                out += '## %s\n' % t.human_name()
                for prop in v:
                    if isinstance(prop.value, basestring):
                        out += '* %s\n' % prop.value
                        continue
                    for kk, vv in prop.value.items():
                        out += '* %s: %s\n' % (kk, vv)
        return out

    def render_overview(self, _):
        #List of components, list of tenants, (tenant, env) -> graph with lights
        return '## Components\n%s\n## Tenants\n%s\n' % (
            ''.join('* %s\n' % n.docs_link().render_html()
                    for n in self.facts().component_tree.children),
            self.facts().tenant_tree.render_tree())


props_base = ('name', 'docs-link', 'redmine')
props_deploy = ('name', 'docs-link', 'redmine', 'servers', 'jenkins')
props_tenant = ('docs-link', 'redmine', 'hiera', 'puppet', 'monitoring')

class Facts(object):
    def __init__(self, x):
        for key in ('url-sets', 'components', 'tenants'):
            if not x.get(key) or not isinstance(x[key], dict):
                raise ValueError('Invalid factsheet, part %s' % key)
        self.url_sets = x['url-sets']
        self.tenant_tree = NodeBase(None, '', '_root', {})
        self.component_tree = NodeBase(None, '', '_root', {})

        self.build_tree(x['components'], self.component_tree, partial(Component, self, ''),
                        lambda x: x.pop('from', None))
        self.build_tree(x['tenants'], self.tenant_tree, partial(Tenant, self),
                        lambda x: x.get('meta', {}).pop('from', None))
        #Link using tenant components' 'from' attributes
        for t in PreOrderIter(self.tenant_tree):
            self.resolve_tenant_component(t)

    def resolve_tenant_component(self, t):
        if t.name == '_root':
            return
        component_parent = t.parent.components if t.parent.name != '_root' else {}
        for name, c in t.components.items():
            if isinstance(c, NodeBase):
                continue
            fro = c.pop('from', None)
            t.components[name] = c = Component(self, t.name, name, c)
            if fro is None:
                if name in component_parent:
                    c.parent = component_parent[name]
                else:
                    p = find_by_attr(self.component_tree, name, name='id')
                    c.parent = p if p else self.component_tree
                continue

            parent = find_by_attr(self.tenant_tree, fro, name='id')
            if parent is None:
                raise KeyError('Unknown tenant %s' % fro)
            while True:
                if parent.name == '_root':
                    raise KeyError('Unknown component %s in tenant %s' % (name, fro))
                if name not in parent.components:
                    parent = parent.parent
                    continue
                if not isinstance(parent.components[name], NodeBase):
                    fro = parent.components[name].pop('from', None)
                    parent.components[name] = Component(self, t.name, name, parent.components[name])
                    if fro is not None:
                        raise ValueError('Not implemented - todo: lazy loading')
                c.parent = parent.components[name]
                break

    @staticmethod
    def build_tree(items, root, ctor, get_from):
        while items:
            stack = [items.popitem()]
            while True:
                if not isinstance(stack[-1], tuple):
                    break
                fro = get_from(stack[-1][1])
                if fro is None:
                    break
                existing = find_by_attr(root, fro, name='id')
                if existing is None:
                    fro_val = items.pop(fro, None)
                    if fro_val is None:
                        raise KeyError('Unknown node %s' % fro)
                    stack.append((fro, fro_val))
                else:
                    stack.append(existing)
            parent = root
            while stack:
                x = stack.pop()
                if not isinstance(x, NodeMixin):
                    x = ctor(x[0], x[1])
                x.parent = parent
                parent = x

    def url_set(self, k):
        if k not in self.url_sets:
            raise KeyError('Unknown url_set %s' % k)
        v = self.url_sets[k]
        if not isinstance(v, UrlSet):
            v = self.url_sets[k] = UrlSet(k, v)
        return v

    def tenant(self, k):
        return find_by_attr(self.tenant_tree, k, name='id')

    def component(self, k):
        return findall_by_attr(self.component_tree, k)

    def component_refs(self, k):
        return [t for t in PreOrderIter(self.tenant_tree)
                if t.name != '_root' and any(x.name == k for (x, _) in t.all_components())]

class Property(object):
    def __init__(self, facts, origin, k, v):
        self.origin = origin
        self.name = k
        if k == 'servers' or k == 'urls':
            if isinstance(v, basestring):
                self.value = facts.url_set(v)
            else:
                self.value = UrlSet(k, v)
        elif k == 'jenkins':
            self.value = Link.maybe(v)
        else:
            self.value = v

class PropDict(dict):
    def render_html(self):
        out = ''
        copy = self.copy()
        url_table = self.render_url_table(copy)
        for (k, name, icon) in PropDict.known_props:
            if k not in copy or not copy[k]:
                continue
            out += '<dt><i class="%s"></i> %s</dt>' % (icon, name)
            for v in copy.pop(k):
                out += self.render_html_single(k, v)
        for k, v in copy.items():
            if k == 'name' or k == 'docs-link' or not v:
                continue
            out += '<dt>%s</dt>' % k
            for vv in copy.pop(k):
                out += self.render_html_single(k, vv)
        return '%s<dl class="prop-list">%s</dl>' % (url_table, out)

    def render_url_table(self, copy): #pylint: disable=no-self-use
        urls = copy.pop('urls', [])
        servers = copy.pop('servers', [])
        if not urls and not servers:
            return ''
        out = '<thead><th></th><th>DEV</th><th>UAT</th><th>PROD</th>'
        for urls in urls + servers:
            if not urls:
                continue
            name = urls.name
            urls = urls.value
            if not isinstance(urls, UrlSet):
                urls = UrlSet('', {'dev': [urls], 'uat': [urls], 'prod': [urls]})
            _, name, icon = [x for x in PropDict.known_props if x[0] == name][0]
            out += '<tr><td><i class="%s"></i> %s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % (
                icon, name, '<br>'.join(urls.dev), '<br>'.join(urls.uat), '<br>'.join(urls.prod))
        return '<table>%s</table>' % out

    def render_html_single(self, k, vv): #pylint: disable=no-self-use, too-many-return-statements
        if k == 'docs-link' or k == 'redmine':
            return '<dd>%s</dd>' % Link.maybe(vv.value).render_html()
        if k == 'repository' or k == 'hiera' or k == 'puppet':
            return '<dd>%s</dd>' % Repo.maybe(vv.value)
        if isinstance(vv.value, Link):
            return '<dd>%s</dd>' % vv.value.render_html()
        if isinstance(vv.value, basestring):
            return '<dd>%s</dd>' % vv.value
        if isinstance(vv.value, dict):
            return ''.join('<dd>%s: %s</dd>' % (k, v) for k, v in vv.value.items())
        return ''.join('<dd>%s</dd>' % x for x in vv.value)

    known_props = [('redmine', 'Redmine', 'fa-fw fab fa-readme'),
                   ('repository', 'Repository', 'fa-fw fab fa-git'),
                   ('urls', 'URLs', 'fa-fw fas fa-bolt'),
                   ('hiera', 'Hiera', 'fa-fw fas fa-sitemap'),
                   ('puppet', 'Puppet', 'fa-fw fas fa-robot'),
                   ('jenkins', 'Jenkins', 'fa-fw fab fab-jenkins'),
                   ('servers', 'Servers', 'fa-fw fas fa-server'),
                   ('monitoring', 'Monitoring', 'fa-fw fas fa-eye'),
                   ('components', 'Components', 'fa-fw fas fa-cogs'),
                   ('tenants', 'Tenants', 'fa-fw fas fa-briefcase')]

class UrlSet(object):
    def __init__(self, k, v):
        v = {kk: asList(vv) for kk, vv in v.items()}
        for key in ('dev', 'uat', 'prod'):
            vv = v[key]
            if not isinstance(vv, list) or not all(isinstance(vvv, basestring) for vvv in vv):
                print(vv)
                raise ValueError('Invalid url set %s, part %s' % (k, key))
        self.name = k
        self.dev = v['dev']
        self.uat = v['uat']
        self.prod = v['prod']

    def render_html(self):
        return '<dl><dt>dev</dt>%s<dt>uat</dt>%s<dt>prod</dt>%s</dl>' % (
            ', '.join(self.dev), ', '.join(self.uat), ', '.join(self.prod))

class NodeBase(NodeMixin):
    def __init__(self, facts, origin, name, props):
        self.id = origin + '_' + name if origin else name
        self.origin = origin
        self.name = name
        self.parent = None
        self.props = {k: [Property(facts, origin, k, vv) for vv in asList(v)]
                      for k, v in props.items()}
        self._all_props = None
        super(NodeBase, self).__init__()

    def human_name(self):
        props = self.all_props()
        return props['name'][0].value if 'name' in props else self.name

    def docs_link(self):
        props = self.all_props()
        name = self.human_name()
        if 'docs-link' not in props:
            return name
        return Link(props['docs-link'][0].value, name)

    def all_props(self, req=()):
        if self._all_props:
            return self._all_props
        props, c = defaultdict(list), self
        while c is not None and c.name != '_root':
            for k, v in c.props.items():
                props[k] += v
            c = c.parent
        reqd = [k for k in req if k not in props]
        if reqd:
            raise KeyError('Required properties %s not found in node %s (id %s)'
                           % (', '.join(reqd), self.name, self.id))
        self._all_props = PropDict(props)
        return self._all_props

    def render_tree(self, indent='\n'):
        if self.name == '_root':
            return ''.join(n.render_tree(indent) for n in self.children)
        return indent + '* ' + self.docs_link().render_html() + \
            (' (' + self.origin + ')' if self.origin else '') + \
            ''.join(n.render_tree(indent + '    ') for n in self.children)

    def __repr__(self):
        return '<NodeBase %s>' % self.id

class Component(NodeBase):
    def __init__(self, facts, origin, k, v):
        if 'from' in v:
            raise ValueError('Logic error in Facts constructor - "from" in Component()')
        super(Component, self).__init__(facts, origin, k, v)

class Tenant(NodeBase):
    def __init__(self, facts, k, v):
        if 'from' in v.get('meta', {}):
            raise ValueError('Logic error in Facts constructor - "from" in Tenant()')
        super(Tenant, self).__init__(facts, '', k, v.pop('meta', {}))
        self.components = {kk: vv for kk, vv in v.items()}

    def all_components(self):
        cs, c = {k: (v, self) for k, v in self.components.items()}, self
        while c.parent is not None and c.parent.name != '_root':
            c = c.parent
            for k, v in c.components.items():
                if k not in cs:
                    cs[k] = (v, c)
        return cs.values()

class Link(object):
    def __init__(self, url, text=None):
        self.url = url
        self.text = url if text is None else text

    def render_html(self):
        return '<a href="%s">%s</a>' % (self.url, self.text)

    @classmethod
    def maybe(cls, url):
        if url.startswith('http:') or url.startswith('https:'):
            return cls(url)
        return url

class Repo(Link):
    def __init__(self, x):
        if x.startswith('http:') or x.startswith('https:'):
            super(Repo, self).__init__(x)
        xs = x.split('/')
        if len(xs) != 2:
            raise ValueError('Invalid repository shorthand "%s"' % x)
        super(Repo, self).__init__(
            "https://redmine.inuits.eu/projects/%s/repository/%s" % (xs[0], xs[1]), x)

    @classmethod
    def maybe(cls, url):
        if len(url.split('/')) != 2 and not url.startswith('http:') and not url.startswith('https:'):
            return url
        return cls(url).render_html()

def asList(x):
    return x if isinstance(x, list) else [x]

def squish(x):
    return [z for y in x for z in y]
