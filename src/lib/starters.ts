import { makeEvent, type DoraEvent, type Op } from '../../shared/ops.js'
import type { Kind } from '../../shared/kinds.js'

/**
 * Skeleton maps for an empty canvas, in Dora's enterprise framing. Plain words, so they read as
 * examples to overwrite rather than jargon to decode.
 */
export type Starter = { id: string; title: string; blurb: string; ops: () => Op[] }

type Spec = [id: string, kind: Kind, title: string, summary: string, actor?: string, system?: string]

function build(mapTitle: string, specs: Spec[], links: [string, string, string?][], workflow: { title: string; summary: string; steps: string[] }): Op[] {
  const events: DoraEvent[] = specs.map(([id, kind, title, summary, actor = '', system = '']) => makeEvent({ id, kind, title, summary, actor, system }))
  return [
    { op: 'setTitle', title: mapTitle },
    ...events.map((event): Op => ({ op: 'addEvent', event })),
    ...links.map(([from, to, label = '']): Op => ({ op: 'addLink', link: { from, to, label } })),
    { op: 'addWorkflow', workflow: { id: 'main', ...workflow } },
  ]
}

export const STARTERS: Starter[] = [
  {
    id: 'journey',
    title: 'Customer journey',
    blurb: 'How a customer signs up and gets value, and what happens behind each step.',
    ops: () => build(
      'How a customer gets started',
      [
        ['signup', 'start', 'Customer signs up', 'A new customer creates an account.', 'Customer'],
        ['signup-page', 'touchpoint', 'Sign-up page', 'Where they enter their name, email and password.', 'Customer', 'Web app'],
        ['sign-in', 'security', 'Check who they are', 'Confirms the email and signs them in safely.', 'System', 'Auth service'],
        ['profile', 'data', 'Account details', 'Name, company and plan, saved for later.', 'System'],
        ['accounts-db', 'storage', 'Customer database', 'Where account details are kept.', 'System', 'Database'],
        ['setup', 'action', 'Customer sets up their workspace', 'They invite teammates and connect their tools.', 'Customer', 'Web app'],
        ['events', 'tracking', 'Sign-up is tracked', 'Records that a new account started, for reports.', 'System', 'Analytics'],
        ['ready', 'outcome', 'Customer is ready to go', 'The workspace is set up and the customer can start.', 'Customer'],
      ],
      [['signup', 'signup-page'], ['signup-page', 'sign-in'], ['sign-in', 'profile'], ['profile', 'accounts-db'], ['sign-in', 'setup'], ['setup', 'events'], ['setup', 'ready']],
      { title: 'New customer gets started', summary: 'From sign-up to a ready workspace.', steps: ['signup', 'signup-page', 'sign-in', 'setup', 'ready'] },
    ),
  },
  {
    id: 'pipeline',
    title: 'Data pipeline',
    blurb: 'How data comes in, splits by type, passes checks, and ends up somewhere useful.',
    ops: () => build(
      'How data moves through the product',
      [
        ['arrives', 'start', 'New data arrives', 'Data shows up from customers and partner systems.', 'System'],
        ['records', 'data', 'Customer records', 'Names, accounts and contact details.', 'System', 'CRM'],
        ['payments', 'data', 'Payments', 'Charges, refunds and invoices.', 'System', 'Payment provider'],
        ['files', 'data', 'Uploaded files', 'Documents customers send in.', 'Customer'],
        ['clean', 'process', 'Check and clean the data', 'Fixes formats and removes duplicates.', 'System'],
        ['access', 'security', 'Check who can see it', 'Only people with permission can open this data.', 'System', 'Access control'],
        ['warehouse', 'storage', 'Data warehouse', 'Where clean data is kept for reports.', 'System', 'Warehouse'],
        ['audit', 'tracking', 'Audit log', 'Records who touched which data, and when.', 'System'],
        ['reports', 'outcome', 'Reports are ready', 'Teams see up-to-date numbers.', 'Customer'],
      ],
      [['arrives', 'records'], ['arrives', 'payments'], ['arrives', 'files'], ['records', 'clean'], ['payments', 'clean'], ['files', 'clean'], ['clean', 'access'], ['access', 'warehouse'], ['access', 'audit'], ['warehouse', 'reports']],
      { title: 'Payment data becomes a report', summary: 'One kind of data, start to finish.', steps: ['arrives', 'payments', 'clean', 'access', 'warehouse', 'reports'] },
    ),
  },
]
