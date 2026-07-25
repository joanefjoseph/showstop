BEGIN;

-- Fixed IDs make local testing predictable and repeatable.
INSERT INTO labels (id, name)
VALUES ('10000000-0000-4000-8000-000000000001', 'HYBE')
ON CONFLICT (id) DO NOTHING;

INSERT INTO users (id, email, full_name, passport_number_hash)
VALUES (
  '20000000-0000-4000-8000-000000000001',
  'testfan@example.com',
  'Test Fan',
  'seed_hash_placeholder'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO events (id, label_id, tour_name, venue_name, event_timestamp, max_tickets_per_fan)
VALUES (
  '30000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'SHOWSTOP WORLD TOUR',
  'Seoul Olympic Stadium',
  NOW() + INTERVAL '30 days',
  2
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO fan_memberships (id, user_id, label_id, membership_tier, is_active, expires_at)
VALUES (
  '40000000-0000-4000-8000-000000000001',
  '20000000-0000-4000-8000-000000000001',
  '10000000-0000-4000-8000-000000000001',
  'GLOBAL_ARMY',
  true,
  NOW() + INTERVAL '365 days'
)
ON CONFLICT (id) DO NOTHING;

INSERT INTO seats (id, event_id, section, row_identifier, seat_number, base_price_cents)
VALUES
  ('50000000-0000-4000-8000-000000000001', '30000000-0000-4000-8000-000000000001', 'A', 'A', 11, 15000),
  ('50000000-0000-4000-8000-000000000002', '30000000-0000-4000-8000-000000000001', 'A', 'A', 12, 15000),
  ('50000000-0000-4000-8000-000000000003', '30000000-0000-4000-8000-000000000001', 'A', 'A', 13, 15000)
ON CONFLICT (id) DO NOTHING;

-- Mark one seat as sold so the available-seats endpoint demonstrates filtering.
INSERT INTO tickets (id, seat_id, user_id, fan_membership_id, purchase_price_cents, secure_device_id)
VALUES (
  '60000000-0000-4000-8000-000000000001',
  '50000000-0000-4000-8000-000000000003',
  '20000000-0000-4000-8000-000000000001',
  '40000000-0000-4000-8000-000000000001',
  15000,
  'seed-device-001'
)
ON CONFLICT (id) DO NOTHING;

COMMIT;
