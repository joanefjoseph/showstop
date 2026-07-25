import express, { Request, Response } from 'express';
import dotenv from 'dotenv';
import ticketRoutes from './routes/ticketRoutes';

dotenv.config();

const app = express();
const port = Number(process.env.PORT) || 3000;

app.use(express.json());

app.get('/health', (_req: Request, res: Response) => {
	res.status(200).json({ status: 'ok' });
});

app.use('/api/tickets', ticketRoutes);

app.listen(port, () => {
	console.log(`Showstop API listening on port ${port}`);
});

export default app;
