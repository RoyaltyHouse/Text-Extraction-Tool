import requests
import os
from databaseUtils import convert_extracted_to_db

base_url = os.getenv("DataBase_URL")



def save_data_to_database(data,file_name,artist_id,original_document_id):
    print(f"[DEBUG] Saving data to database at /parsed-contracts")
    
    # Convert the extracted data to the database schema
    database_data = convert_extracted_to_db(data,file_name,artist_id,original_document_id)

    try:
        response = requests.post(f"{base_url}/parsed-contracts", json=database_data)
        response.raise_for_status()  # Raise an error for bad responses
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[DEBUG] Error saving data to database: {e}")
        return {"error": str(e)}
    

def get_data_from_database():
    try:
        print(f"[DEBUG] Fetching data from database at /parsed-contracts")
         # Fetch data from the database
         # Assuming the endpoint returns a JSON response
         # Adjust the endpoint as per your API design
        response = requests.get(f"{base_url}/parsed-contracts")
        response.raise_for_status()  # Raise an error for bad responses
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"[DEBUG] Error fetching data from database: {e}")
        return {"error": str(e)}
    
def delete_data_from_database(contract_id):
    try:
        response = requests.delete(f"{base_url}/parsed-contracts/{contract_id}")
        response.raise_for_status()  # Raise an error for bad responses
        return response.json()
    except requests.exceptions.RequestException as e:
        print(f"Error deleting data from database: {e}")
        return {"error": str(e)}
    

