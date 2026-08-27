-- 1. Music Labels / Agencies (e.g., HYBE, JYP)
CREATE TABLE IF NOT EXISTS labels (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. Users / Global Fans
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    full_name VARCHAR(100) NOT NULL,
    tm_member_id VARCHAR(100), -- Third-party TM user identifier if bound
    passport_number_hash VARCHAR(64), -- For strict physical verification at the venue gates
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. Paid Fan-Club Memberships (The Gatekeeper)
CREATE TABLE IF NOT EXISTS fan_memberships (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    label_id UUID REFERENCES labels(id) ON DELETE CASCADE,
    membership_tier VARCHAR(50) DEFAULT 'GLOBAL_ARMY',
    is_active BOOLEAN DEFAULT true,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT unique_user_label_membership UNIQUE (user_id, label_id)
);

-- 4. Concert Events (Mapped to Ticketmaster Event IDs)
CREATE TABLE IF NOT EXISTS events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tm_event_id VARCHAR(100) UNIQUE NOT NULL, -- e.g., 'vvG1YZ94bJbE_K'
    label_id UUID REFERENCES labels(id),
    tour_name VARCHAR(150) NOT NULL,
    venue_name VARCHAR(150) NOT NULL,
    event_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    max_tickets_per_fan INT DEFAULT 2 -- Hard cap to prevent multi-ticket harvesting
);

-- 5. Physical Seats / General Admission Slots
CREATE TABLE IF NOT EXISTS seats (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id UUID REFERENCES events(id) ON DELETE CASCADE,
    tm_seat_id VARCHAR(100), -- Ticketmaster proprietary seat identifier
    section VARCHAR(50) NOT NULL,
    row_identifier VARCHAR(10) NOT NULL,
    seat_number INT NOT NULL,
    base_price_cents INT NOT NULL, -- Stored in cents to avoid floating-point errors
    CONSTRAINT unique_event_seat UNIQUE (event_id, section, row_identifier, seat_number)
);

-- 6. Ticket Ledger (Finalized Orders with TM Commit ID & SafeTix)
CREATE TABLE IF NOT EXISTS tickets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tm_order_id VARCHAR(100) UNIQUE, -- Generated upon Cart Commit on Ticketmaster
    seat_id UUID REFERENCES seats(id),
    user_id UUID REFERENCES users(id),
    fan_membership_id UUID REFERENCES fan_memberships(id), -- Double validation lock
    purchase_price_cents INT NOT NULL,
    safetix_token TEXT, -- Token passed to TM Secure Entry SDK
    issued_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    secure_device_id VARCHAR(255) NOT NULL -- Links ticket to a specific physical smartphone
);